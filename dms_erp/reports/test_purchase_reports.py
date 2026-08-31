import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.purchase import po_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.reports import purchase_reports
from dms_erp.warehouse.test_fixtures import ensure_company, make_item, make_supplier


class TestPurchaseReports(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_purchase()
		cls.supplier = make_supplier("Purchase Reports Test Supplier")
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
