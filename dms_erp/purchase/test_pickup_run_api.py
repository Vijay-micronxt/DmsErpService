import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.purchase import pickup_run_api, po_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.warehouse.test_fixtures import ensure_company, make_item, make_supplier


class TestPickupRunApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_purchase()
		cls.item = make_item("PICKUP-TEST-ITEM", "Vitrified")
		cls.supplier = make_supplier("Pickup Run Test Supplier")
		cls.other_supplier = make_supplier("Pickup Run Other Supplier")
		cls.small_truck = pickup_run_api.create_vehicle_type("Small Truck", 100)
		cls.big_truck = pickup_run_api.create_vehicle_type("Big Truck", 900)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _ready_line(self, supplier=None, ordered_qty=1000, ready_qty=500):
		po = po_api.create_purchase_order(
			item=self.item, ordered_qty=ordered_qty, supplier=supplier or self.supplier, expected_ready_date="2026-09-01"
		)
		line_id = po["lines"][0]["id"]
		po_api.set_line_ready(po["id"], line_id, ready_qty)
		return line_id

	def test_create_vehicle_type_and_list(self):
		vt = pickup_run_api.create_vehicle_type("Mini Truck", 250)
		self.assertEqual(vt["capacityBoxes"], 250)
		names = {v["name"] for v in pickup_run_api.list_vehicle_types()}
		self.assertIn("Mini Truck", names)

	def test_create_pickup_run_computes_total_boxes(self):
		line = self._ready_line(ready_qty=400)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 200}]
		)
		self.assertEqual(run["status"], "Draft")
		self.assertEqual(run["totalBoxes"], 200)
		self.assertEqual(run["lines"][0]["item"], self.item)

	def test_create_pickup_run_rejects_over_vehicle_capacity(self):
		line = self._ready_line(ready_qty=500)
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.small_truck["id"], lines=[{"purchase_order_item": line, "qty": 150}]
			)

	def test_create_pickup_run_rejects_qty_over_ready_qty(self):
		line = self._ready_line(ready_qty=100)
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 150}]
			)

	def test_create_pickup_run_rejects_line_from_a_different_supplier(self):
		line = self._ready_line(supplier=self.other_supplier, ready_qty=500)
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 100}]
			)

	def test_second_draft_run_cannot_overbook_qty_already_reserved_by_the_first(self):
		line = self._ready_line(ready_qty=300)
		pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 250}]
		)
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 100}]
			)

	def test_add_pickup_run_line_recomputes_total_and_rejects_once_dispatched(self):
		line1 = self._ready_line(ready_qty=300)
		line2 = self._ready_line(ready_qty=300)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line1, "qty": 100}]
		)
		run = pickup_run_api.add_pickup_run_line(run["id"], line2, 150)
		self.assertEqual(run["totalBoxes"], 250)
		self.assertEqual(len(run["lines"]), 2)

		pickup_run_api.advance_pickup_run_status(run["id"], "Dispatched")
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.add_pickup_run_line(run["id"], line1, 10)

	def test_dispatch_creates_one_inward_truck_per_line_linked_back_to_the_run(self):
		line1 = self._ready_line(ready_qty=300)
		line2 = self._ready_line(ready_qty=300)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier,
			vehicle_type=self.big_truck["id"],
			vehicle_number="GJ-05-XX-9999",
			lines=[{"purchase_order_item": line1, "qty": 120}, {"purchase_order_item": line2, "qty": 80}],
		)

		dispatched = pickup_run_api.advance_pickup_run_status(run["id"], "Dispatched")
		self.assertEqual(dispatched["status"], "Dispatched")

		trucks = frappe.get_all("Inward Truck", filters={"pickup_run": run["id"]}, fields=["boxes", "vehicle_number", "purchase_order_item"])
		self.assertEqual(len(trucks), 2)
		self.assertEqual(sum(t.boxes for t in trucks), 200)
		self.assertTrue(all(t.vehicle_number == "GJ-05-XX-9999" for t in trucks))

	def test_invalid_status_transition_is_rejected(self):
		line = self._ready_line(ready_qty=200)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 100}]
		)
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.advance_pickup_run_status(run["id"], "Completed")

	def test_write_requires_purchase_warehouse_or_management_role(self):
		line = self._ready_line(ready_qty=200)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 100}]
			)
