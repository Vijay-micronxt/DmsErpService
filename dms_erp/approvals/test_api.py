import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.approvals import api as approvals_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, order_api, quotation_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier
from dms_erp.warehouse.utils import default_company

TEST_PASSWORD = "Pa$$w0rd123!"


def _make_sales_only_user(email: str) -> str:
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "Sales",
			"send_welcome_email": 0,
			"new_password": TEST_PASSWORD,
			"roles": [{"role": "DMS Sales"}],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


class TestApprovalsApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Approvals Test Supplier")
		cls.dealer = make_dealer("Approvals Test Dealer")

		cls.priced_item = make_item("APR-PRICED", "Vitrified")
		pricing_api.ensure_price_record(cls.priced_item, cls.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=cls.priced_item, final_price=500, reason="Launch")

		cls.second_item = make_item("APR-SECOND", "Vitrified")
		pricing_api.ensure_price_record(cls.second_item, cls.supplier, 200, 25, "2026-08-01")
		pricing_api.approve_price(item=cls.second_item, final_price=250, reason="Launch")

		cls.sales_user = _make_sales_only_user("approvals.sales@pacific.test")

	def tearDown(self):
		frappe.set_user("Administrator")

	# ---------------- gate_channel_override, exercised via create_quotation ----------------

	def test_authorized_user_override_applies_immediately_and_logs_an_approved_request(self):
		before = frappe.db.count("Approval Request")

		quotation = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12, channel="Bulk"
		)
		self.assertEqual(quotation["channel"], "Bulk")

		after = frappe.db.count("Approval Request")
		self.assertEqual(after, before + 1)

		approval = frappe.get_last_doc("Approval Request")
		self.assertEqual(approval.trigger_type, "Channel Override")
		self.assertEqual(approval.status, "Approved")
		self.assertEqual(approval.reference_doctype, "Quotation")
		self.assertEqual(approval.reference_name, quotation["id"])
		self.assertEqual(approval.decided_by, "Administrator")

	def test_unauthorized_user_override_is_queued_instead_of_applied(self):
		frappe.set_user(self.sales_user)
		before = frappe.db.count("Quotation")

		result = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12, channel="Bulk"
		)

		self.assertTrue(result["approvalRequired"])
		self.assertEqual(result["approval"]["status"], "Pending")
		self.assertEqual(result["approval"]["triggerType"], "Channel Override")
		self.assertIsNone(result["approval"]["referenceName"])
		# The gated document must not exist yet -- only Management deciding it does.
		self.assertEqual(frappe.db.count("Quotation"), before)

	def test_matching_channel_never_raises_an_approval_request(self):
		before = frappe.db.count("Approval Request")
		quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12
		)  # channel left unset -> auto-classifies to Retail, no override at all
		self.assertEqual(frappe.db.count("Approval Request"), before)

	# ---------------- decide_approval ----------------

	def test_approving_a_queued_channel_override_creates_the_quotation(self):
		frappe.set_user(self.sales_user)
		queued = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 11}], markup_pct=12, channel="Bulk"
		)
		approval_id = queued["approval"]["id"]

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Approved", note="Confirmed with the dealer.")

		self.assertEqual(decided["status"], "Approved")
		self.assertEqual(decided["referenceDoctype"], "Quotation")
		self.assertIsNotNone(decided["referenceName"])

		quotation = quotation_api.get_quotation(decided["referenceName"])
		self.assertEqual(quotation["channel"], "Bulk")

	def test_rejecting_a_queued_channel_override_creates_nothing(self):
		frappe.set_user(self.sales_user)
		queued = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 12}], markup_pct=12, channel="Bulk"
		)
		approval_id = queued["approval"]["id"]
		before = frappe.db.count("Quotation")

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Rejected", note="Not a genuine bulk order.")

		self.assertEqual(decided["status"], "Rejected")
		self.assertIsNone(decided["referenceName"])
		self.assertEqual(frappe.db.count("Quotation"), before)

	def test_deciding_twice_is_rejected(self):
		frappe.set_user(self.sales_user)
		queued = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 13}], markup_pct=12, channel="Bulk"
		)
		approval_id = queued["approval"]["id"]

		frappe.set_user("Administrator")
		approvals_api.decide_approval(approval_id, "Rejected")
		with self.assertRaises(frappe.ValidationError):
			approvals_api.decide_approval(approval_id, "Approved")

	def test_only_management_can_decide(self):
		frappe.set_user(self.sales_user)
		queued = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 14}], markup_pct=12, channel="Bulk"
		)
		approval_id = queued["approval"]["id"]

		with self.assertRaises(frappe.PermissionError):
			approvals_api.decide_approval(approval_id, "Approved")

	def test_only_management_can_list_or_read_approvals(self):
		frappe.set_user(self.sales_user)
		with self.assertRaises(frappe.PermissionError):
			approvals_api.list_pending_approvals()

	# ---------------- list_pending_approvals ----------------

	def test_list_pending_approvals_filters_by_status_and_trigger_type(self):
		frappe.set_user(self.sales_user)
		quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 15}], markup_pct=12, channel="Bulk"
		)
		frappe.set_user("Administrator")

		result = approvals_api.list_pending_approvals(trigger_type="Channel Override", status="Pending")
		self.assertGreaterEqual(result["total"], 1)
		self.assertTrue(all(item["status"] == "Pending" for item in result["items"]))
		self.assertTrue(all(item["triggerType"] == "Channel Override" for item in result["items"]))

	# ---------------- gate_authorized_action, exercised via quotation line edits (trigger #5) ----------------

	def _make_quotation(self, qty=10):
		return quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": qty}], markup_pct=12
		)

	def test_authorized_user_can_update_a_line_immediately_and_logs_an_approved_request(self):
		quotation = self._make_quotation(qty=20)
		before = frappe.db.count("Approval Request")

		updated = quotation_api.update_quotation_line_qty(quotation["id"], self.priced_item, 30)
		self.assertEqual(updated["lines"][0]["qty"], 30)
		self.assertNotEqual(updated["id"], quotation["id"])  # amend cycle -> new document name

		after = frappe.db.count("Approval Request")
		self.assertEqual(after, before + 1)

		approval = frappe.get_last_doc("Approval Request")
		self.assertEqual(approval.trigger_type, "Amend Or Cancel Submitted Document")
		self.assertEqual(approval.status, "Approved")
		self.assertEqual(approval.reference_doctype, "Quotation")
		self.assertEqual(approval.reference_name, updated["id"])

	def test_unauthorized_user_line_edit_is_queued_instead_of_applied(self):
		quotation = self._make_quotation(qty=21)

		frappe.set_user(self.sales_user)
		result = quotation_api.update_quotation_line_qty(quotation["id"], self.priced_item, 40)

		self.assertTrue(result["approvalRequired"])
		self.assertEqual(result["approval"]["status"], "Pending")
		self.assertEqual(result["approval"]["triggerType"], "Amend Or Cancel Submitted Document")
		self.assertEqual(result["approval"]["referenceDoctype"], "Quotation")
		self.assertEqual(result["approval"]["referenceName"], quotation["id"])

		# The quotation itself must be untouched -- still the original doc, original qty.
		frappe.set_user("Administrator")
		untouched = quotation_api.get_quotation(quotation["id"])
		self.assertEqual(untouched["lines"][0]["qty"], 21)

	def test_approving_a_queued_line_edit_amends_the_quotation(self):
		quotation = self._make_quotation(qty=22)

		frappe.set_user(self.sales_user)
		queued = quotation_api.update_quotation_line_qty(quotation["id"], self.priced_item, 50)
		approval_id = queued["approval"]["id"]

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Approved")

		self.assertEqual(decided["status"], "Approved")
		self.assertEqual(decided["referenceDoctype"], "Quotation")
		self.assertNotEqual(decided["referenceName"], quotation["id"])  # the amended copy

		amended = quotation_api.get_quotation(decided["referenceName"])
		self.assertEqual(amended["lines"][0]["qty"], 50)

	def test_rejecting_a_queued_line_edit_leaves_the_quotation_untouched(self):
		quotation = self._make_quotation(qty=23)

		frappe.set_user(self.sales_user)
		queued = quotation_api.update_quotation_line_qty(quotation["id"], self.priced_item, 60)
		approval_id = queued["approval"]["id"]

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Rejected")
		self.assertEqual(decided["status"], "Rejected")

		untouched = quotation_api.get_quotation(quotation["id"])
		self.assertEqual(untouched["lines"][0]["qty"], 23)

	def test_authorized_user_can_add_and_remove_a_line_immediately(self):
		quotation = self._make_quotation(qty=24)

		added = quotation_api.add_quotation_line(quotation["id"], self.second_item, 5)
		self.assertEqual(len(added["lines"]), 2)

		removed = quotation_api.remove_quotation_line(added["id"], self.priced_item)
		self.assertEqual(len(removed["lines"]), 1)
		self.assertEqual(removed["lines"][0]["itemCode"], self.second_item)

	# ---------------- gate_authorized_action, exercised via order cancellation (trigger #5) ----------------

	def _make_order(self, qty=10):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=qty, source="Phone")
		return order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": qty}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)

	def test_authorized_user_can_cancel_an_order_immediately_and_logs_an_approved_request(self):
		order = self._make_order(qty=16)
		before = frappe.db.count("Approval Request")

		cancelled = order_api.advance_order_stage(order["id"], "Cancelled")
		self.assertEqual(cancelled["id"], order["id"])

		after = frappe.db.count("Approval Request")
		self.assertEqual(after, before + 1)

		approval = frappe.get_last_doc("Approval Request")
		self.assertEqual(approval.trigger_type, "Amend Or Cancel Submitted Document")
		self.assertEqual(approval.status, "Approved")
		self.assertEqual(approval.reference_doctype, "Sales Order")
		self.assertEqual(approval.reference_name, order["id"])

	def test_unauthorized_user_order_cancel_is_queued_instead_of_applied(self):
		order = self._make_order(qty=17)

		frappe.set_user(self.sales_user)
		result = order_api.advance_order_stage(order["id"], "Cancelled")

		self.assertTrue(result["approvalRequired"])
		self.assertEqual(result["approval"]["status"], "Pending")
		self.assertEqual(result["approval"]["referenceDoctype"], "Sales Order")
		self.assertEqual(result["approval"]["referenceName"], order["id"])

		frappe.set_user("Administrator")
		untouched = order_api.get_order(order["id"])
		self.assertNotEqual(untouched["stage"], "Cancelled")

	def test_approving_a_queued_order_cancel_cancels_it(self):
		order = self._make_order(qty=18)

		frappe.set_user(self.sales_user)
		queued = order_api.advance_order_stage(order["id"], "Cancelled")
		approval_id = queued["approval"]["id"]

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Approved")
		self.assertEqual(decided["status"], "Approved")
		self.assertEqual(decided["referenceName"], order["id"])

		self.assertEqual(frappe.db.get_value("Sales Order", order["id"], "custom_fulfillment_stage"), "Cancelled")

	# ---------------- gate_discount_over_price_list, exercised via create_quotation/create_order (trigger #4) ----------------

	def test_authorized_user_discount_applies_immediately_and_logs_an_approved_request(self):
		before = frappe.db.count("Approval Request")

		quotation = quotation_api.create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 5}],
			markup_pct=12,
		)
		self.assertEqual(quotation["lines"][0]["discountPercentage"], 5)

		after = frappe.db.count("Approval Request")
		self.assertEqual(after, before + 1)

		approval = frappe.get_last_doc("Approval Request")
		self.assertEqual(approval.trigger_type, "Discount Over Price List")
		self.assertEqual(approval.status, "Approved")
		self.assertEqual(approval.reference_doctype, "Quotation")
		self.assertEqual(approval.reference_name, quotation["id"])
		self.assertEqual(approval.decided_by, "Administrator")

	def test_unauthorized_user_discount_is_queued_instead_of_applied(self):
		frappe.set_user(self.sales_user)
		before = frappe.db.count("Quotation")

		result = quotation_api.create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 1}],
			markup_pct=12,
		)

		self.assertTrue(result["approvalRequired"])
		self.assertEqual(result["approval"]["status"], "Pending")
		self.assertEqual(result["approval"]["triggerType"], "Discount Over Price List")
		self.assertIsNone(result["approval"]["referenceName"])
		# Even a 1% discount is an override -- BRD C.7.3 sets no threshold.
		self.assertEqual(frappe.db.count("Quotation"), before)

	def test_zero_discount_never_raises_an_approval_request(self):
		before = frappe.db.count("Approval Request")
		quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 0}], markup_pct=12
		)
		quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12
		)  # discount_percentage omitted entirely
		self.assertEqual(frappe.db.count("Approval Request"), before)

	def test_approving_a_queued_discount_creates_the_quotation(self):
		frappe.set_user(self.sales_user)
		queued = quotation_api.create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 10}],
			markup_pct=12,
		)
		approval_id = queued["approval"]["id"]

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Approved")

		self.assertEqual(decided["status"], "Approved")
		self.assertEqual(decided["referenceDoctype"], "Quotation")
		self.assertIsNotNone(decided["referenceName"])

		quotation = quotation_api.get_quotation(decided["referenceName"])
		self.assertEqual(quotation["lines"][0]["discountPercentage"], 10)

	def test_rejecting_a_queued_discount_creates_nothing(self):
		frappe.set_user(self.sales_user)
		queued = quotation_api.create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 10}],
			markup_pct=12,
		)
		approval_id = queued["approval"]["id"]
		before = frappe.db.count("Quotation")

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Rejected")

		self.assertEqual(decided["status"], "Rejected")
		self.assertIsNone(decided["referenceName"])
		self.assertEqual(frappe.db.count("Quotation"), before)

	def test_authorized_user_doing_both_channel_and_discount_overrides_logs_two_approved_requests(self):
		before = frappe.db.count("Approval Request")

		quotation = quotation_api.create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 5}],
			markup_pct=12,
			channel="Bulk",
		)

		after = frappe.db.count("Approval Request")
		self.assertEqual(after, before + 2)

		trigger_types = set(
			frappe.get_all(
				"Approval Request",
				filters={"reference_name": quotation["id"]},
				pluck="trigger_type",
			)
		)
		self.assertEqual(trigger_types, {"Channel Override", "Discount Over Price List"})

	def test_authorized_user_order_discount_applies_immediately_and_logs_an_approved_request(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=10, source="Phone")
		before = frappe.db.count("Approval Request")

		order = order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 5}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)
		self.assertEqual(order["lines"][0]["discountPercentage"], 5)

		after = frappe.db.count("Approval Request")
		self.assertEqual(after, before + 1)
		approval = frappe.get_last_doc("Approval Request")
		self.assertEqual(approval.trigger_type, "Discount Over Price List")
		self.assertEqual(approval.reference_doctype, "Sales Order")
		self.assertEqual(approval.reference_name, order["id"])

	def test_unauthorized_user_order_discount_is_queued_instead_of_applied(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=10, source="Phone")
		before = frappe.db.count("Sales Order")

		frappe.set_user(self.sales_user)
		result = order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 2}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)

		self.assertTrue(result["approvalRequired"])
		self.assertEqual(result["approval"]["triggerType"], "Discount Over Price List")
		self.assertEqual(frappe.db.count("Sales Order"), before)

	# ---------------- gate_credit_limit, exercised via create_order/convert_to_order (trigger #1) ----------------

	def _make_credit_dealer(self, name: str, limit: float) -> str:
		dealer = make_dealer(name)
		doc = frappe.get_doc("Customer", dealer)
		doc.append("credit_limits", {"company": default_company(), "credit_limit": limit})
		doc.save(ignore_permissions=True)
		return dealer

	def test_order_within_credit_limit_never_raises_an_approval_request(self):
		dealer = self._make_credit_dealer("Credit Test Dealer Within Limit", limit=10000)
		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=self.priced_item, qty=5, source="Phone")
		before = frappe.db.count("Approval Request")

		order_api.create_order(
			dealer=dealer,
			lines=[{"item": self.priced_item, "qty": 5}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)  # 5 * 500 = 2500, well under the 10000 limit

		self.assertEqual(frappe.db.count("Approval Request"), before)

	def test_dealer_with_no_credit_limit_never_gates_even_for_a_large_order(self):
		dealer = make_dealer("Credit Test Dealer No Limit")
		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=self.priced_item, qty=1000, source="Phone")
		before = frappe.db.count("Approval Request")

		order_api.create_order(
			dealer=dealer,
			lines=[{"item": self.priced_item, "qty": 1000}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)

		self.assertEqual(frappe.db.count("Approval Request"), before)

	def test_authorized_user_order_over_credit_limit_applies_immediately_and_logs_an_approved_request(self):
		dealer = self._make_credit_dealer("Credit Test Dealer Authorized Order", limit=1000)
		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=self.priced_item, qty=10, source="Phone")
		before = frappe.db.count("Approval Request")

		order = order_api.create_order(
			dealer=dealer,
			lines=[{"item": self.priced_item, "qty": 10}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)  # 10 * 500 = 5000, over the 1000 limit

		after = frappe.db.count("Approval Request")
		self.assertEqual(after, before + 1)

		approval = frappe.get_last_doc("Approval Request")
		self.assertEqual(approval.trigger_type, "Credit Limit Exceeded")
		self.assertEqual(approval.status, "Approved")
		self.assertEqual(approval.reference_doctype, "Sales Order")
		self.assertEqual(approval.reference_name, order["id"])

	def test_unauthorized_user_order_over_credit_limit_is_queued_instead_of_applied(self):
		dealer = self._make_credit_dealer("Credit Test Dealer Unauthorized Order", limit=1000)
		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=self.priced_item, qty=10, source="Phone")
		before = frappe.db.count("Sales Order")

		frappe.set_user(self.sales_user)
		result = order_api.create_order(
			dealer=dealer,
			lines=[{"item": self.priced_item, "qty": 10}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)

		self.assertTrue(result["approvalRequired"])
		self.assertEqual(result["approval"]["status"], "Pending")
		self.assertEqual(result["approval"]["triggerType"], "Credit Limit Exceeded")
		self.assertIsNone(result["approval"]["referenceName"])
		self.assertEqual(frappe.db.count("Sales Order"), before)

	def test_approving_a_queued_credit_limit_order_creates_it(self):
		dealer = self._make_credit_dealer("Credit Test Dealer Approve Order", limit=1000)
		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=self.priced_item, qty=10, source="Phone")

		frappe.set_user(self.sales_user)
		queued = order_api.create_order(
			dealer=dealer,
			lines=[{"item": self.priced_item, "qty": 10}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)
		approval_id = queued["approval"]["id"]

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Approved")

		self.assertEqual(decided["status"], "Approved")
		self.assertEqual(decided["referenceDoctype"], "Sales Order")
		self.assertIsNotNone(decided["referenceName"])
		self.assertEqual(order_api.get_order(decided["referenceName"])["dealerId"], dealer)

	def test_rejecting_a_queued_credit_limit_order_creates_nothing(self):
		dealer = self._make_credit_dealer("Credit Test Dealer Reject Order", limit=1000)
		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=self.priced_item, qty=10, source="Phone")
		before = frappe.db.count("Sales Order")

		frappe.set_user(self.sales_user)
		queued = order_api.create_order(
			dealer=dealer,
			lines=[{"item": self.priced_item, "qty": 10}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)
		approval_id = queued["approval"]["id"]

		frappe.set_user("Administrator")
		decided = approvals_api.decide_approval(approval_id, "Rejected")
		self.assertEqual(decided["status"], "Rejected")
		self.assertEqual(frappe.db.count("Sales Order"), before)

	def test_authorized_user_quotation_conversion_over_credit_limit_logs_an_approved_request(self):
		dealer = self._make_credit_dealer("Credit Test Dealer Authorized Convert", limit=1000)
		quotation = quotation_api.create_quotation(
			dealer=dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12
		)  # priced well over the 1000 limit
		before = frappe.db.count("Approval Request")

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch="2026-09-01")

		after = frappe.db.count("Approval Request")
		self.assertEqual(after, before + 1)
		approval = frappe.get_last_doc("Approval Request")
		self.assertEqual(approval.trigger_type, "Credit Limit Exceeded")
		self.assertEqual(approval.status, "Approved")
		self.assertEqual(approval.reference_name, order["id"])

	def test_unauthorized_user_quotation_conversion_over_credit_limit_is_queued_instead_of_applied(self):
		dealer = self._make_credit_dealer("Credit Test Dealer Unauthorized Convert", limit=1000)
		quotation = quotation_api.create_quotation(
			dealer=dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12
		)
		before = frappe.db.count("Sales Order")

		frappe.set_user(self.sales_user)
		result = quotation_api.convert_to_order(quotation["id"], expected_dispatch="2026-09-01")

		self.assertTrue(result["approvalRequired"])
		self.assertEqual(result["approval"]["triggerType"], "Credit Limit Exceeded")
		self.assertEqual(frappe.db.count("Sales Order"), before)
