import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, order_api, picking_api
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_dealer, make_item, make_supplier


class TestPickingApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Picking Test Supplier")
		cls.dealer = make_dealer("Picking Test Dealer")
		cls.item = make_item("PICK-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")
		cls.bay = make_bay("PICK-A-01", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def _make_order_in_picking(self, qty):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=qty, source="Phone")
		order = order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": qty}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)
		order_api.advance_order_stage(order["id"], "Picking")
		return order

	def test_auto_allocate_reads_real_stock(self):
		allocation_api.create_allocation(
			item=self.item, batch_no="PICK-BATCH-1", total_qty=15, lines=[{"bay": "PICK-A-01", "qty": 15}], supplier=self.supplier
		)
		order = self._make_order_in_picking(qty=25)
		task = picking_api.list_pick_tasks(order["id"])[0]

		allocated = picking_api.auto_allocate(task["id"])
		self.assertEqual(allocated["allocated"], 15)  # only 15 in stock against a 25 qty task
		self.assertEqual(allocated["status"], "Allocated")

	def test_patch_task_assigns_picker(self):
		order = self._make_order_in_picking(qty=5)
		task = picking_api.list_pick_tasks(order["id"])[0]

		updated = picking_api.patch_task(task["id"], {"picker": "Administrator", "status": "Picked"})
		self.assertEqual(updated["picker"], "Administrator")
		self.assertEqual(updated["status"], "Picked")

	def test_write_requires_warehouse_or_management_role(self):
		order = self._make_order_in_picking(qty=5)
		task = picking_api.list_pick_tasks(order["id"])[0]

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			picking_api.auto_allocate(task["id"])
