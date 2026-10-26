from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase import pickup_run_api, po_api, route_plan_api, transporter_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.sales import order_api
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_dealer_addresses,
	make_item,
	make_supplier,
)

_ORIGIN = (12.9716, 77.5946)


def _set_route_origin():
	settings = frappe.get_single("DMS Purchase Settings")
	settings.route_origin_latitude = _ORIGIN[0]
	settings.route_origin_longitude = _ORIGIN[1]
	settings.save(ignore_permissions=True)


def _set_supplier_gps(supplier: str, lat: float, lng: float):
	frappe.db.set_value("Supplier", supplier, {"custom_latitude": lat, "custom_longitude": lng})


def _fake_optimize_result(order, legs, polyline="fake-polyline"):
	return {"order": order, "legs": [{"distance_km": d, "duration_minutes": m} for d, m in legs], "polyline": polyline}


def _fake_legs(legs, polyline="fake-polyline"):
	return {"legs": [{"distance_km": d, "duration_minutes": m} for d, m in legs], "polyline": polyline}


class TestRoutePlanApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_purchase()
		_set_route_origin()

		cls.item = make_item("ROUTE-PLAN-TEST-ITEM", "Vitrified")
		frappe.db.set_value("Item", cls.item, "custom_weight_per_box_kg", 20)
		cls.supplier_a = make_supplier("Route Plan Test Supplier A")
		cls.supplier_b = make_supplier("Route Plan Test Supplier B")
		_set_supplier_gps(cls.supplier_a, 22.8181, 70.8328)
		_set_supplier_gps(cls.supplier_b, 22.4707, 70.0577)

		transporter = transporter_api.create_transporter(
			"Route Plan Test Transporter",
			vehicles=[
				{"vehicle_number": "GJ-RTP-0001", "vehicle_type": "Truck", "mode": "Road", "capacity_tonnes": 25}
			],
		)
		cls.transporter = transporter["id"]
		cls.vehicle_row = transporter["vehicles"][0]["id"]

		setup_pricing()
		cls.dealer = make_dealer("Route Plan Test Dealer")
		cls.billing_address, cls.shipping_address = make_dealer_addresses(cls.dealer)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _ready_line(self, supplier, ordered_qty=1000, ready_qty=500, drop_ship_sales_order=None):
		po = po_api.create_purchase_order(
			item=self.item,
			ordered_qty=ordered_qty,
			supplier=supplier,
			expected_ready_date="2026-09-01",
			drop_ship_sales_order=drop_ship_sales_order,
		)
		line_id = po["lines"][0]["id"]
		po_api.set_line_ready(po["id"], line_id, ready_qty)
		return line_id

	def _submitted_sales_order(self):
		item = make_item("ROUTE-PLAN-DROPSHIP-SO-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier_a, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		return order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": item, "qty": 5}],
			expected_dispatch="2027-01-15",
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
		)

	def _two_stop_plan(self, qty_a=100, qty_b=100):
		line_a = self._ready_line(self.supplier_a)
		line_b = self._ready_line(self.supplier_b)
		return route_plan_api.create_route_plan(
			transporter=self.transporter,
			transporter_vehicle=self.vehicle_row,
			stops=[{"supplier": self.supplier_a}, {"supplier": self.supplier_b}],
			lines=[
				{"purchase_order_item": line_a, "qty": qty_a},
				{"purchase_order_item": line_b, "qty": qty_b},
			],
		)

	def test_create_route_plan_with_multiple_stops_and_suppliers(self):
		plan = self._two_stop_plan()
		self.assertEqual(plan["status"], "Draft")
		self.assertEqual({s["supplier"] for s in plan["stops"]}, {self.supplier_a, self.supplier_b})
		self.assertEqual([s["sequenceNo"] for s in plan["stops"]], [1, 2])
		self.assertEqual(len(plan["lines"]), 2)
		self.assertGreater(plan["totalWeightKg"], 0)

	def test_a_line_whose_supplier_has_no_stop_is_rejected(self):
		line_a = self._ready_line(self.supplier_a)
		with self.assertRaises(frappe.ValidationError):
			route_plan_api.create_route_plan(
				transporter=self.transporter,
				transporter_vehicle=self.vehicle_row,
				stops=[{"supplier": self.supplier_b}],
				lines=[{"purchase_order_item": line_a, "qty": 50}],
			)

	def test_a_duplicate_supplier_stop_is_rejected(self):
		line_a = self._ready_line(self.supplier_a)
		with self.assertRaises(frappe.ValidationError):
			route_plan_api.create_route_plan(
				transporter=self.transporter,
				transporter_vehicle=self.vehicle_row,
				stops=[{"supplier": self.supplier_a}, {"supplier": self.supplier_a}],
				lines=[{"purchase_order_item": line_a, "qty": 10}],
			)

	def test_optimize_route_reorders_stops_and_sets_totals(self):
		plan = self._two_stop_plan()
		with patch("dms_erp.purchase.routing_client.optimize_waypoints") as mock_optimize:
			mock_optimize.return_value = _fake_optimize_result(
				[1, 0], [(120, 150), (80, 100)], polyline="encoded-abc"
			)
			optimized = route_plan_api.optimize_route(plan["id"])

		mock_optimize.assert_called_once()
		self.assertEqual([s["supplier"] for s in optimized["stops"]], [self.supplier_b, self.supplier_a])
		self.assertEqual([s["sequenceNo"] for s in optimized["stops"]], [1, 2])
		self.assertEqual(optimized["totalDistanceKm"], 200)
		self.assertEqual(optimized["totalDurationMinutes"], 250)
		self.assertEqual(optimized["routePolyline"], "encoded-abc")

	def test_get_route_plan_has_no_polyline_before_the_first_optimize(self):
		plan = self._two_stop_plan()
		self.assertIsNone(route_plan_api.get_route_plan(plan["id"])["routePolyline"])

	def test_resequence_route_stops_accepts_manual_order_and_recomputes_legs(self):
		plan = self._two_stop_plan()
		with patch("dms_erp.purchase.routing_client.optimize_waypoints") as mock_optimize:
			mock_optimize.return_value = _fake_optimize_result([0, 1], [(50, 60), (60, 70)])
			plan = route_plan_api.optimize_route(plan["id"])

		stop_ids = [s["id"] for s in plan["stops"]]
		expected_suppliers = [s["supplier"] for s in reversed(plan["stops"])]
		manual_order = list(reversed(stop_ids))

		with patch("dms_erp.purchase.routing_client.compute_fixed_order_legs") as mock_fixed:
			mock_fixed.return_value = _fake_legs([(90, 100), (70, 80)], polyline="encoded-xyz")
			resequenced = route_plan_api.resequence_route_stops(plan["id"], manual_order)

		mock_fixed.assert_called_once()
		# A resequence rebuilds the child table, so row ids change on save --
		# supplier order (what the staff member actually chose) is what matters.
		self.assertEqual([s["supplier"] for s in resequenced["stops"]], expected_suppliers)
		self.assertEqual([s["sequenceNo"] for s in resequenced["stops"]], [1, 2])
		self.assertEqual(resequenced["totalDistanceKm"], 160)
		self.assertEqual(resequenced["totalDurationMinutes"], 180)
		# Resequencing re-measures the now-fixed order, so the polyline is refreshed too, not
		# left pointing at the stale pre-optimize-then-manually-reordered geometry.
		self.assertEqual(resequenced["routePolyline"], "encoded-xyz")

	def test_dispatch_nets_ready_qty_against_both_pickup_runs_and_route_plans(self):
		# A Draft Pickup Run reserves 400 of supplier_a's 500 ready boxes first...
		line_a = self._ready_line(self.supplier_a, ready_qty=500)
		vt = pickup_run_api.create_vehicle_type("Route Plan Test Vehicle Type", 1000)
		pickup_run_api.create_pickup_run(supplier=self.supplier_a, vehicle_type=vt["id"], lines=[{"purchase_order_item": line_a, "qty": 400}])

		# ...so a Route Plan trying to book more than the remaining 100 must fail.
		with self.assertRaises(frappe.ValidationError):
			route_plan_api.create_route_plan(
				transporter=self.transporter,
				transporter_vehicle=self.vehicle_row,
				stops=[{"supplier": self.supplier_a}],
				lines=[{"purchase_order_item": line_a, "qty": 150}],
			)

		# But booking within what's left still succeeds.
		plan = route_plan_api.create_route_plan(
			transporter=self.transporter,
			transporter_vehicle=self.vehicle_row,
			stops=[{"supplier": self.supplier_a}],
			lines=[{"purchase_order_item": line_a, "qty": 100}],
		)
		self.assertEqual(plan["lines"][0]["qty"], 100)

		# And now a second Pickup Run for the same line has nothing left at all.
		with self.assertRaises(frappe.ValidationError):
			pickup_run_api.create_pickup_run(supplier=self.supplier_a, vehicle_type=vt["id"], lines=[{"purchase_order_item": line_a, "qty": 1}])

	def test_dispatch_creates_one_inward_truck_per_line_tagged_with_route_plan(self):
		plan = self._two_stop_plan(qty_a=10, qty_b=20)
		dispatched = route_plan_api.advance_route_plan_status(plan["id"], "Dispatched")

		self.assertEqual(dispatched["status"], "Dispatched")
		trucks = frappe.get_all("Inward Truck", filters={"route_plan": plan["id"]}, fields=["supplier", "boxes", "vehicle_number"])
		self.assertEqual(len(trucks), 2)
		self.assertEqual({t.supplier for t in trucks}, {self.supplier_a, self.supplier_b})
		self.assertTrue(all(t.vehicle_number == "GJ-RTP-0001" for t in trucks))

	def test_dispatch_warns_on_overload_but_still_dispatches(self):
		# 30kg/box x 1000 boxes = 30 tonnes, over the 25-tonne test vehicle.
		heavy_item = make_item("ROUTE-PLAN-HEAVY-ITEM", "Vitrified")
		frappe.db.set_value("Item", heavy_item, "custom_weight_per_box_kg", 30)
		po = po_api.create_purchase_order(item=heavy_item, ordered_qty=1000, supplier=self.supplier_a, expected_ready_date="2026-09-01")
		line_id = po["lines"][0]["id"]
		po_api.set_line_ready(po["id"], line_id, 1000)

		plan = route_plan_api.create_route_plan(
			transporter=self.transporter,
			transporter_vehicle=self.vehicle_row,
			stops=[{"supplier": self.supplier_a}],
			lines=[{"purchase_order_item": line_id, "qty": 1000}],
		)
		self.assertEqual(plan["totalWeightKg"], 30000)

		dispatched = route_plan_api.advance_route_plan_status(plan["id"], "Dispatched")
		self.assertEqual(dispatched["status"], "Dispatched")
		self.assertIn("overloadWarning", dispatched)

	def test_cannot_optimize_a_non_draft_plan(self):
		plan = self._two_stop_plan()
		route_plan_api.advance_route_plan_status(plan["id"], "Dispatched")
		with self.assertRaises(frappe.ValidationError):
			route_plan_api.optimize_route(plan["id"])

	def test_create_route_plan_requires_purchase_or_warehouse_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			route_plan_api.create_route_plan(
				transporter=self.transporter, transporter_vehicle=self.vehicle_row, stops=[{"supplier": self.supplier_a}], lines=[]
			)

	def test_a_supplier_with_no_gps_cannot_be_added_as_a_stop(self):
		no_gps_supplier = make_supplier("Route Plan No GPS Supplier")
		line = self._ready_line(no_gps_supplier)
		with self.assertRaises(frappe.ValidationError):
			route_plan_api.create_route_plan(
				transporter=self.transporter,
				transporter_vehicle=self.vehicle_row,
				stops=[{"supplier": no_gps_supplier}],
				lines=[{"purchase_order_item": line, "qty": 10}],
			)

	def test_create_route_plan_rejects_a_drop_ship_po_line(self):
		# BRD C.3.3 -- a drop-ship PO ships straight from the supplier to the end
		# customer and never passes through this app's own route plan.
		so = self._submitted_sales_order()
		line = self._ready_line(self.supplier_a, ready_qty=200, drop_ship_sales_order=so["id"])
		with self.assertRaises(frappe.ValidationError):
			route_plan_api.create_route_plan(
				transporter=self.transporter,
				transporter_vehicle=self.vehicle_row,
				stops=[{"supplier": self.supplier_a}],
				lines=[{"purchase_order_item": line, "qty": 100}],
			)

	def test_add_route_plan_line_rejects_a_drop_ship_po_line(self):
		line_a = self._ready_line(self.supplier_a, ready_qty=300)
		plan = route_plan_api.create_route_plan(
			transporter=self.transporter,
			transporter_vehicle=self.vehicle_row,
			stops=[{"supplier": self.supplier_a}],
			lines=[{"purchase_order_item": line_a, "qty": 100}],
		)

		so = self._submitted_sales_order()
		drop_ship_line = self._ready_line(self.supplier_a, ready_qty=200, drop_ship_sales_order=so["id"])
		with self.assertRaises(frappe.ValidationError):
			route_plan_api.add_route_plan_line(plan["id"], drop_ship_line, 50)
