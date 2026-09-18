import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse import unallocated_stock_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_item, make_supplier


class TestUnallocatedStockApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.item = make_item("UNALLOC-TEST-ITEM", "Vitrified")
		cls.supplier = make_supplier("Unallocated Test Supplier")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 400, 25, "2026-08-01")
		cls.bay = make_bay("UNALLOC-A-01", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_record_unallocated_stock_defaults_to_pending(self):
		entry = unallocated_stock_api.record_unallocated_stock(item=self.item, qty=5, location="Near loading dock, left corner")
		self.assertEqual(entry["status"], "Pending")
		self.assertEqual(entry["qty"], 5)
		self.assertEqual(entry["scannedBy"], "Administrator")

	def test_record_unallocated_stock_requires_a_location(self):
		with self.assertRaises(frappe.ValidationError):
			unallocated_stock_api.record_unallocated_stock(item=self.item, qty=5, location="   ")

	def test_list_unallocated_stock_defaults_to_pending_only(self):
		entry = unallocated_stock_api.record_unallocated_stock(item=self.item, qty=3, location="Behind Bay C")
		unallocated_stock_api.consolidate_unallocated_stock(entry["id"], remarks="Merged into ALLOC-BATCH-1 in Bay A")

		pending = unallocated_stock_api.list_unallocated_stock()
		self.assertNotIn(entry["id"], [r["id"] for r in pending["items"]])

		everything = unallocated_stock_api.list_unallocated_stock(status=None)
		self.assertIn(entry["id"], [r["id"] for r in everything["items"]])

	def test_allocate_unallocated_stock_creates_a_real_bay_allocation(self):
		entry = unallocated_stock_api.record_unallocated_stock(item=self.item, qty=8, location="Loose near Bay A")

		resolved = unallocated_stock_api.allocate_unallocated_stock(entry["id"], bay="UNALLOC-A-01", supplier=self.supplier)

		self.assertEqual(resolved["status"], "Allocated")
		self.assertTrue(resolved["resolvedAllocation"])
		alloc_status = frappe.db.get_value("Bay Allocation", resolved["resolvedAllocation"], "status")
		self.assertEqual(alloc_status, "Confirmed")

	def test_consolidate_unallocated_stock_requires_remarks(self):
		entry = unallocated_stock_api.record_unallocated_stock(item=self.item, qty=2, location="Loose near Bay A")
		with self.assertRaises(frappe.ValidationError):
			unallocated_stock_api.consolidate_unallocated_stock(entry["id"], remarks="")

	def test_cannot_resolve_an_already_resolved_entry_twice(self):
		entry = unallocated_stock_api.record_unallocated_stock(item=self.item, qty=2, location="Loose near Bay A")
		unallocated_stock_api.consolidate_unallocated_stock(entry["id"], remarks="Merged into existing lot")

		with self.assertRaises(frappe.ValidationError):
			unallocated_stock_api.consolidate_unallocated_stock(entry["id"], remarks="Try again")
		with self.assertRaises(frappe.ValidationError):
			unallocated_stock_api.allocate_unallocated_stock(entry["id"], bay="UNALLOC-A-01", supplier=self.supplier)

	def test_write_requires_warehouse_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			unallocated_stock_api.record_unallocated_stock(item=self.item, qty=1, location="Anywhere")
