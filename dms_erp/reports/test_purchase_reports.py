import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.purchase import po_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.reports import purchase_reports
from dms_erp.sales import inquiry_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestPurchaseReports(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_purchase()
		cls.supplier = make_supplier("Purchase Reports Test Supplier")
		cls.dealer = make_dealer("Purchase Reports Test Dealer")
		cls.item = make_item("PREPORT-TEST-ITEM", "Vitrified")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_reorder_planning_report_filters_actionable_only(self):
		full = purchase_reports.reorder_planning_report()
		actionable = purchase_reports.reorder_planning_report(actionable_only=True)
		self.assertGreaterEqual(len(full), len(actionable))
		self.assertTrue(all(r["suggestedQty"] > 0 for r in actionable))

	def test_purchase_pickup_plan_reflects_ready_qty(self):
		po = po_api.create_purchase_order(item=self.item, ordered_qty=500, supplier=self.supplier, expected_ready_date="2026-09-01")
		line_id = po["lines"][0]["id"]
		po_api.set_line_ready(po["id"], line_id, 300)

		result = purchase_reports.purchase_pickup_plan()
		row = next(r for r in result["rows"] if r["line"] == line_id)
		self.assertEqual(row["readyQty"], 300)
		self.assertGreaterEqual(result["summary"]["totalReadyQty"], 300)

	def test_inquiry_to_po_mapping_report_joins_inquiry_and_po(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=40, source="Phone")
		inquiry_api.update_inquiry(inquiry["id"], {"status": "Out of Stock"})
		po = inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], supplier=self.supplier, expected_ready_date="2026-09-01")

		result = purchase_reports.inquiry_to_po_mapping_report()
		row = next(r for r in result if r["inquiryId"] == inquiry["id"])
		self.assertEqual(row["poId"], po["id"])
		self.assertEqual(row["poOrderedQty"], 40)
		self.assertEqual(row["inquiryStatus"], "Mapped to PO")

	def test_inquiry_to_po_mapping_report_excludes_direct_pos(self):
		po = po_api.create_purchase_order(item=self.item, ordered_qty=100, supplier=self.supplier, expected_ready_date="2026-09-01")

		result = purchase_reports.inquiry_to_po_mapping_report()
		self.assertNotIn(po["id"], [r["poId"] for r in result])

	def test_po_pending_report_flags_overdue(self):
		po_api.create_purchase_order(item=self.item, ordered_qty=200, supplier=self.supplier, expected_ready_date=add_days(today(), -3))

		result = purchase_reports.po_pending_report(supplier=self.supplier, overdue_only=True)
		self.assertGreaterEqual(result["summary"]["overdueCount"], 1)
		self.assertTrue(all(r["daysOverdue"] > 0 for r in result["rows"]))
