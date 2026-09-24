import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_months, today

from dms_erp.catalog import dealer_catalog_api, sample_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_dealer, make_item, make_supplier


class TestSampleApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Sample Test Supplier")
		cls.dealer = make_dealer("Sample Test Dealer")
		cls.item = make_item("SAMPLE-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 400, 25, "2026-08-01")
		cls.bay = make_bay("SAMPLE-MAIN-01", bay_type="main", categories=["Vitrified"])
		allocation_api.create_allocation(
			item=cls.item,
			batch_no="SAMPLE-BATCH-1",
			total_qty=50,
			lines=[{"bay": "SAMPLE-MAIN-01", "qty": 50}],
			supplier=cls.supplier,
		)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _approved_request(self, dealer=None, qty=1):
		request = sample_api.create_sample_request(item=self.item, dealer=dealer or self.dealer, qty=qty)
		return sample_api.approve_sample_request(request["id"], approve=True)

	def test_full_lifecycle_issues_generates_qr_and_grants_catalog_visibility(self):
		# An empty Dealer Catalog record so is_visible reflects real assignment rather
		# than the "no record yet -> sees everything" fallback (dealer_catalog_api's
		# own documented behavior for an unassigned dealer).
		frappe.get_doc({"doctype": "Dealer Catalog", "dealer": self.dealer, "items": []}).insert(ignore_permissions=True)
		self.assertFalse(dealer_catalog_api.is_visible(self.dealer, self.item))

		approved = self._approved_request()
		self.assertEqual(approved["approvalStatus"], "Approved")

		result = sample_api.issue_sample(approved["id"], bay="SAMPLE-MAIN-01", batch_no="SAMPLE-BATCH-1")

		self.assertEqual(result["sampleRequest"]["approvalStatus"], "Issued")
		self.assertTrue(result["sampleRequest"]["sampleQrCode"].startswith(f"{self.item}-"))
		self.assertEqual(result["placement"]["item"], self.item)
		self.assertEqual(result["placement"]["dealer"], self.dealer)
		self.assertEqual(result["placement"]["status"], "Active")

		# BRD C.1.5 -- issuing the sample is what grants catalog visibility.
		self.assertTrue(dealer_catalog_api.is_visible(self.dealer, self.item))

		item_doc = frappe.get_doc("Item", self.item)
		row = next(r for r in item_doc.custom_dealer_codes if r.dealer == self.dealer)
		self.assertTrue(row.sample_issued)

	def test_qr_code_differs_per_dealer_for_the_same_item(self):
		other_dealer = make_dealer("Sample Test Dealer Two")

		first = sample_api.issue_sample(self._approved_request()["id"], bay="SAMPLE-MAIN-01", batch_no="SAMPLE-BATCH-1")
		second = sample_api.issue_sample(
			self._approved_request(dealer=other_dealer)["id"], bay="SAMPLE-MAIN-01", batch_no="SAMPLE-BATCH-1"
		)

		self.assertNotEqual(first["sampleRequest"]["sampleQrCode"], second["sampleRequest"]["sampleQrCode"])

	def test_create_sample_request_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			sample_api.create_sample_request(item=self.item, dealer=self.dealer)

	def test_approve_and_issue_require_warehouse_or_management_role(self):
		request = sample_api.create_sample_request(item=self.item, dealer=self.dealer)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			sample_api.approve_sample_request(request["id"], approve=True)

	def test_approve_sample_request_refuses_a_non_pending_request(self):
		approved = self._approved_request()
		with self.assertRaises(frappe.ValidationError):
			sample_api.approve_sample_request(approved["id"], approve=True)

	def test_issue_sample_refuses_a_non_approved_request(self):
		request = sample_api.create_sample_request(item=self.item, dealer=self.dealer)
		with self.assertRaises(frappe.ValidationError):
			sample_api.issue_sample(request["id"], bay="SAMPLE-MAIN-01", batch_no="SAMPLE-BATCH-1")

	def test_record_reconciliation_defaults_action_from_response(self):
		result = sample_api.issue_sample(self._approved_request()["id"], bay="SAMPLE-MAIN-01", batch_no="SAMPLE-BATCH-1")
		slip = result["placement"]["id"]

		still_displayed = sample_api.record_reconciliation(slip, "Still Displayed")
		self.assertEqual(still_displayed["action"], "Keep")
		self.assertEqual(sample_api.get_display_placement(slip)["status"], "Active")

		removed = sample_api.record_reconciliation(slip, "Removed")
		self.assertEqual(removed["action"], "Pullback")
		self.assertEqual(sample_api.get_display_placement(slip)["status"], "Removed")

	def test_record_reconciliation_accepts_an_explicit_action_override(self):
		result = sample_api.issue_sample(self._approved_request()["id"], bay="SAMPLE-MAIN-01", batch_no="SAMPLE-BATCH-1")
		slip = result["placement"]["id"]

		recon = sample_api.record_reconciliation(slip, "Still Displayed", action="Put-up Replacement")
		self.assertEqual(recon["action"], "Put-up Replacement")

	def test_pullback_display_records_condition_and_decision_without_moving_stock(self):
		result = sample_api.issue_sample(self._approved_request()["id"], bay="SAMPLE-MAIN-01", batch_no="SAMPLE-BATCH-1")
		slip = result["placement"]["id"]

		pulled = sample_api.pullback_display(slip, condition="Fair", decision="Clearance")

		self.assertEqual(pulled["status"], "Pulled Back")
		self.assertEqual(pulled["condition"], "Fair")
		self.assertEqual(pulled["pullbackDecision"], "Clearance")

	def test_monitoring_reminders_fire_once_per_interval_crossed(self):
		result = sample_api.issue_sample(self._approved_request()["id"], bay="SAMPLE-MAIN-01", batch_no="SAMPLE-BATCH-1")
		slip = result["placement"]["id"]

		sent = sample_api.send_display_monitoring_reminders()
		self.assertEqual(sent, 0)  # just placed today, no interval crossed yet

		frappe.db.set_value("Display Placement Slip", slip, "placement_date", add_months(today(), -4))
		sent = sample_api.send_display_monitoring_reminders()
		self.assertEqual(sent, 1)  # crossed the 3-month mark
		self.assertEqual(frappe.db.get_value("Display Placement Slip", slip, "last_reminder_interval_months"), 3)

		sent_again = sample_api.send_display_monitoring_reminders()
		self.assertEqual(sent_again, 0)  # 3-month reminder already sent, 6 not reached yet
