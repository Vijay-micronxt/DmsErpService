import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.approvals import api as approvals_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import quotation_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier

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
