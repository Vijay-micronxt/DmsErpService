import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase import pickup_run_api, po_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.sales import order_api
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_dealer_addresses,
	make_item,
	make_supplier,
)


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
		setup_pricing()
		cls.dealer = make_dealer("Pickup Run Test Dealer")
		cls.billing_address, cls.shipping_address = make_dealer_addresses(cls.dealer)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _ready_line(self, supplier=None, ordered_qty=1000, ready_qty=500, drop_ship_sales_order=None):
		po = po_api.create_purchase_order(
			item=self.item, ordered_qty=ordered_qty, supplier=supplier or self.supplier,
			# Relative, not a fixed date: a fixed one rots into the past and ERPNext then
			# refuses the PO ("Reqd by Date cannot be before Transaction Date").
			expected_ready_date=add_days(today(), 30),
			drop_ship_sales_order=drop_ship_sales_order,
		)
		line_id = po["lines"][0]["id"]
		po_api.set_line_ready(po["id"], line_id, ready_qty)
		return line_id

	def _submitted_sales_order(self):
		item = make_item("PICKUP-DROPSHIP-SO-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		return order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": item, "qty": 5}],
			expected_dispatch=add_days(today(), 30),
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
		)

	def test_create_vehicle_type_and_list(self):
		vt = pickup_run_api.create_vehicle_type("Mini Truck", 250)
		self.assertEqual(vt["capacityBoxes"], 250)
		names = {v["name"] for v in pickup_run_api.list_vehicle_types()["items"]}
		self.assertIn("Mini Truck", names)

	def test_list_vehicle_types_is_paginated(self):
		before = pickup_run_api.list_vehicle_types()
		baseline_total = before["total"]

		for name in ("Page Truck A", "Page Truck B", "Page Truck C"):
			pickup_run_api.create_vehicle_type(name, 100)

		page = pickup_run_api.list_vehicle_types(limit=2, offset=0)
		self.assertEqual(page["total"], baseline_total + 3)
		self.assertEqual(len(page["items"]), 2)
		self.assertEqual(page["limit"], 2)
		self.assertEqual(page["offset"], 0)

	def test_list_pickup_runs_is_paginated_and_filters_by_supplier_and_status(self):
		# Other tests in this class create runs for the same supplier, so count from a baseline.
		before_total = pickup_run_api.list_pickup_runs(supplier=self.supplier)["total"]
		before_dispatched = pickup_run_api.list_pickup_runs(supplier=self.supplier, status="Dispatched")["total"]
		line1 = self._ready_line(ready_qty=300)
		line2 = self._ready_line(ready_qty=300)
		line3 = self._ready_line(supplier=self.other_supplier, ready_qty=300)
		pickup_run_api.create_pickup_run(supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line1, "qty": 100}])
		run2 = pickup_run_api.create_pickup_run(supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line2, "qty": 100}])
		pickup_run_api.create_pickup_run(supplier=self.other_supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line3, "qty": 100}])
		pickup_run_api.advance_pickup_run_status(run2["id"], "Dispatched")

		page = pickup_run_api.list_pickup_runs(supplier=self.supplier, limit=1, offset=0)
		self.assertEqual(page["total"], before_total + 2)
		self.assertEqual(len(page["items"]), 1)

		by_status = pickup_run_api.list_pickup_runs(supplier=self.supplier, status="Dispatched")
		self.assertEqual(by_status["total"], before_dispatched + 1)
		self.assertIn(run2["id"], {r["id"] for r in by_status["items"]})

	def test_create_pickup_run_computes_total_boxes(self):
		line = self._ready_line(ready_qty=400)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 200}]
		)
		self.assertEqual(run["status"], "Draft")
		self.assertEqual(run["totalBoxes"], 200)
		self.assertEqual(run["lines"][0]["item"], self.item)

	def test_pickup_run_line_carries_the_items_weight(self):
		frappe.db.set_value("Item", self.item, "custom_weight_per_box_kg", 28)
		line = self._ready_line(ready_qty=400)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 200}]
		)
		self.assertEqual(run["lines"][0]["weightPerBoxKg"], 28)
		self.assertEqual(run["lines"][0]["totalWeightKg"], 5600)

	def test_pickup_run_line_carries_the_items_pieces_sqft_and_sqm(self):
		frappe.db.set_value("Item", self.item, "custom_pieces_per_box", 4)
		frappe.db.set_value("Item", self.item, "custom_sqft_per_box", 15.5)
		line = self._ready_line(ready_qty=400)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 200}]
		)
		self.assertEqual(run["lines"][0]["piecesPerBox"], 4)
		self.assertEqual(run["lines"][0]["totalPieces"], 800)
		self.assertEqual(run["lines"][0]["sqftPerBox"], 15.5)
		self.assertEqual(run["lines"][0]["totalSqft"], 3100)
		self.assertAlmostEqual(run["lines"][0]["sqmPerBox"], 1.44, places=2)
		self.assertAlmostEqual(run["lines"][0]["totalSqm"], 288, places=2)

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

	def test_rejects_two_lines_in_one_request_that_together_overbook_the_same_po_line(self):
		line = self._ready_line(ready_qty=200)
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier,
				vehicle_type=self.big_truck["id"],
				lines=[{"purchase_order_item": line, "qty": 150}, {"purchase_order_item": line, "qty": 150}],
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

	def test_create_pickup_run_rejects_a_line_missing_purchase_order_item(self):
		# Real production mistake: a caller sent {"purchase_order": po_name, "qty": ...} --
		# the PO's own name, not a specific line's -- and a bare dict lookup crashed with
		# an unhandled KeyError instead of a clean validation message.
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order": "PUR-ORD-2026-00003", "qty": 20}]
			)

	def test_create_pickup_run_rejects_a_line_missing_qty(self):
		line = self._ready_line(ready_qty=200)
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line}]
			)

	def test_write_requires_purchase_warehouse_or_management_role(self):
		line = self._ready_line(ready_qty=200)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 100}]
			)

	def test_brd_fleet_is_seeded_once_with_tonnes_and_mode(self):
		from dms_erp.purchase.setup import BRD_FLEET, seed_brd_fleet

		seed_brd_fleet()
		seed_brd_fleet()  # idempotent
		for name, tonnes, mode in BRD_FLEET:
			self.assertEqual(frappe.db.count("Vehicle Type", {"name": name}), 1)
			self.assertEqual(frappe.db.get_value("Vehicle Type", name, ["capacity_tonnes", "mode"]), (tonnes, mode))

	def test_tonnage_vehicle_rejects_a_run_heavier_than_its_capacity(self):
		# BRD C.1.7: capacity is in tonnes. 28 kg/box: 150 boxes = 4.2 t fits 5 t, 200 = 5.6 t doesn't.
		frappe.db.set_value("Item", self.item, "custom_weight_per_box_kg", 28)
		vt = pickup_run_api.create_vehicle_type("Tonnage Test 5 T", capacity_tonnes=5, mode="Road")
		self.assertIsNone(vt["capacityBoxes"])
		self.assertEqual(vt["capacityTonnes"], 5)

		ok_line = self._ready_line(ready_qty=400)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=vt["id"], lines=[{"purchase_order_item": ok_line, "qty": 150}]
		)
		self.assertEqual(run["totalBoxes"], 150)

		heavy_line = self._ready_line(ready_qty=400)
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=vt["id"], lines=[{"purchase_order_item": heavy_line, "qty": 200}]
			)

	def test_vehicle_type_needs_some_capacity(self):
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_vehicle_type("No Capacity Truck")

	def test_create_pickup_run_rejects_a_drop_ship_po_line(self):
		# BRD C.3.3 -- a drop-ship PO ships straight from the supplier to the end
		# customer and never passes through this app's own pickup flow.
		so = self._submitted_sales_order()
		line = self._ready_line(ready_qty=200, drop_ship_sales_order=so["id"])
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(
				supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line, "qty": 100}]
			)

	def test_add_pickup_run_line_rejects_a_drop_ship_po_line(self):
		line1 = self._ready_line(ready_qty=300)
		run = pickup_run_api.create_pickup_run(
			supplier=self.supplier, vehicle_type=self.big_truck["id"], lines=[{"purchase_order_item": line1, "qty": 100}]
		)

		so = self._submitted_sales_order()
		drop_ship_line = self._ready_line(ready_qty=200, drop_ship_sales_order=so["id"])
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.add_pickup_run_line(run["id"], drop_ship_line, 50)
