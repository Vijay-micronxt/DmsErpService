import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse import allocation_api, inward_api, stock_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_item, make_supplier
from dms_erp.warehouse.setup import setup_warehouse


class TestAllocation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.item = make_item("ALLOC-TEST-ITEM", "Vitrified")
		cls.supplier = make_supplier("Allocation Test Supplier")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 400, 25, "2026-08-01")
		cls.bay_a = make_bay("ALLOC-A-01", categories=["Vitrified"])
		cls.bay_b = make_bay("ALLOC-A-02", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_allocation_splits_across_bays_and_posts_receipt(self):
		result = allocation_api.create_allocation(
			item=self.item,
			batch_no="ALLOC-BATCH-1",
			total_qty=100,
			lines=[{"bay": "ALLOC-A-01", "qty": 60}, {"bay": "ALLOC-A-02", "qty": 40}],
			supplier=self.supplier,
		)

		self.assertEqual(result["status"], "Confirmed")
		self.assertTrue(result["purchaseReceipt"])
		self.assertTrue(frappe.db.get_value("Purchase Receipt", result["purchaseReceipt"], "docstatus") == 1)

		stock = stock_api.list_stock(item=self.item)
		by_bay = {row["bayId"]: row["boxes"] for row in stock if row["batchNumber"] == "ALLOC-BATCH-1"}
		bay_a_name = frappe.db.get_value("Warehouse", {"custom_bay_code": "ALLOC-A-01"}, "name")
		bay_b_name = frappe.db.get_value("Warehouse", {"custom_bay_code": "ALLOC-A-02"}, "name")
		self.assertEqual(by_bay[bay_a_name], 60)
		self.assertEqual(by_bay[bay_b_name], 40)

	def test_create_allocation_rejects_mismatched_line_total(self):
		with self.assertRaises(frappe.ValidationError):
			allocation_api.create_allocation(
				item=self.item,
				batch_no="ALLOC-BATCH-2",
				total_qty=100,
				lines=[{"bay": "ALLOC-A-01", "qty": 60}],
				supplier=self.supplier,
			)

	def test_create_allocation_rejects_over_capacity_line(self):
		with self.assertRaises(frappe.ValidationError):
			allocation_api.create_allocation(
				item=self.item,
				batch_no="ALLOC-BATCH-3",
				total_qty=5000,
				lines=[{"bay": "ALLOC-A-01", "qty": 5000}],
				supplier=self.supplier,
			)

	def test_suggest_bays_prefers_matching_category_and_free_capacity(self):
		suggestions = stock_api.suggest_bays(category="Vitrified", qty=50)
		codes = [s["bay"]["code"] for s in suggestions["main"]]
		self.assertIn("ALLOC-A-01", codes)

	def test_allocation_lifecycle_with_inward_truck(self):
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=20, lr_number="LR-ALLOC-1")
		self.assertEqual(truck["status"], "Scheduled")

		inward_api.advance_truck(truck["id"], "Unloading")

		alloc = allocation_api.create_allocation(
			item=self.item,
			batch_no="ALLOC-BATCH-4",
			total_qty=20,
			lines=[{"bay": "ALLOC-A-01", "qty": 20}],
			inward_truck=truck["id"],
		)
		self.assertEqual(alloc["inwardTruck"], truck["id"])

		printed = allocation_api.mark_allocation_printed(alloc["id"])
		self.assertEqual(printed["status"], "Printed")

		placed = allocation_api.confirm_putaway(alloc["id"])
		self.assertEqual(placed["status"], "Placed")

		trucks = {t["id"]: t for t in inward_api.list_trucks()}
		self.assertEqual(trucks[truck["id"]]["status"], "Put-away")

	def test_resolve_scan_bay_code(self):
		result = allocation_api.resolve_scan("ALLOC-A-01")
		self.assertTrue(result["ok"])
		self.assertEqual(result["kind"], "bay")

	def test_list_allocations_and_get_allocation(self):
		created = allocation_api.create_allocation(
			item=self.item,
			batch_no="ALLOC-BATCH-5",
			total_qty=15,
			lines=[{"bay": "ALLOC-A-01", "qty": 15}],
			supplier=self.supplier,
		)

		fetched = allocation_api.get_allocation(created["id"])
		self.assertEqual(fetched, created)

		listed = allocation_api.list_allocations(item=self.item)
		self.assertIn(created["id"], [a["id"] for a in listed])

		by_status = allocation_api.list_allocations(status="Confirmed")
		self.assertIn(created["id"], [a["id"] for a in by_status])
