import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse import allocation_api, stock_api, transfer_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_item, make_supplier
from dms_erp.warehouse.setup import setup_warehouse


class TestTransfer(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.item = make_item("XFER-TEST-ITEM", "Vitrified")
		cls.supplier = make_supplier("Transfer Test Supplier")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 400, 25, "2026-08-01")
		cls.main_bay = make_bay("XFER-MAIN-01", bay_type="main", categories=["Vitrified"])
		cls.buffer_bay = make_bay("XFER-BUF-01", bay_type="buffer", categories=["Vitrified"])

		allocation_api.create_allocation(
			item=cls.item,
			batch_no="XFER-BATCH-1",
			total_qty=100,
			lines=[{"bay": "XFER-MAIN-01", "qty": 100}],
			supplier=cls.supplier,
		)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_transfer_moves_qty_between_bays(self):
		transfer_api.transfer_stock(
			from_bay="XFER-MAIN-01",
			to_bay="XFER-BUF-01",
			item=self.item,
			batch_no="XFER-BATCH-1",
			qty=30,
			transfer_type="Main→Buffer",
			reason="Consolidation",
		)

		stock = stock_api.list_stock(item=self.item)
		by_bay = {row["bayId"]: row["boxes"] for row in stock if row["batchNumber"] == "XFER-BATCH-1"}
		main_name = frappe.db.get_value("Warehouse", {"custom_bay_code": "XFER-MAIN-01"}, "name")
		buf_name = frappe.db.get_value("Warehouse", {"custom_bay_code": "XFER-BUF-01"}, "name")
		self.assertEqual(by_bay[main_name], 70)
		self.assertEqual(by_bay[buf_name], 30)

	def test_transfer_rejects_insufficient_source_stock(self):
		with self.assertRaises(frappe.ValidationError):
			transfer_api.transfer_stock(
				from_bay="XFER-MAIN-01",
				to_bay="XFER-BUF-01",
				item=self.item,
				batch_no="XFER-BATCH-1",
				qty=99999,
				transfer_type="Main→Buffer",
				reason="Consolidation",
			)

	def test_transfer_requires_warehouse_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			transfer_api.transfer_stock(
				from_bay="XFER-MAIN-01",
				to_bay="XFER-BUF-01",
				item=self.item,
				batch_no="XFER-BATCH-1",
				qty=1,
				transfer_type="Main→Buffer",
				reason="Consolidation",
			)
