import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.reports import warehouse_reports
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_item, make_supplier


class TestWarehouseReports(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Warehouse Reports Test Supplier")
		cls.item = make_item("WREPORT-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		cls.bay = make_bay("WREPORT-A-01", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_bay_occupancy_report_includes_summary(self):
		allocation_api.create_allocation(
			item=self.item, batch_no="WREPORT-BATCH-1", total_qty=100, lines=[{"bay": "WREPORT-A-01", "qty": 100}], supplier=self.supplier
		)

		result = warehouse_reports.bay_occupancy_report(bay_type="main")
		row = next(r for r in result["rows"] if r["code"] == "WREPORT-A-01")
		self.assertEqual(row["occupiedBoxes"], 100)
		self.assertEqual(result["summary"]["bayCount"], len(result["rows"]))

	def test_visual_stock_balance_nests_lots_under_their_bay(self):
		allocation_api.create_allocation(
			item=self.item, batch_no="WREPORT-BATCH-2", total_qty=50, lines=[{"bay": "WREPORT-A-01", "qty": 50}], supplier=self.supplier
		)

		result = warehouse_reports.visual_stock_balance()
		bay_row = next(b for b in result if b["code"] == "WREPORT-A-01")
		self.assertTrue(any(l["batchNumber"] == "WREPORT-BATCH-2" for l in bay_row["lots"]))
