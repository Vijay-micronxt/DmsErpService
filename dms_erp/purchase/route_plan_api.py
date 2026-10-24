"""Route Plan (BRD C.5) — multi-stop, multi-supplier pickup route planning
across the Morbi->Bengaluru corridor, distinct from the existing single-
supplier `Pickup Run` (pickup_run_api.py), which stays exactly as-is for the
simple "book one truck to one supplier" case. A Route Plan instead groups
several suppliers' ready PO lines into one truck/container run, with each
stop's sequence and leg distance/time computed by `routing_client` (Google
Maps Routes API) rather than typed in by hand.

Follows the unguarded-core/whitelisted-wrapper split used throughout
`sales/*_api.py` (CLAUDE.md: new mutating endpoints likely to need
approval-gating later should follow it even if not gated yet) -- note this
is NOT matching existing Purchase-module style: neither Pickup Run nor
Reorder Plan use the split today. BRD C.11 trigger #5 (amend/cancel a
submitted document) is a plausible future fit for gating a
Dispatched->Cancelled transition, not built here.

`transporter_vehicle` is a Transporter Vehicle *row name* (Data field, not a
Link -- child-table rows aren't real Link targets in this app, same
convention as Pickup Run Item.purchase_order_item), resolved the same way
transporter_api.py already resolves one. This deliberately links to the real
C.1.7 tonnage/mode fleet master, not Pickup Run's disconnected box-capacity
`Vehicle Type`.

Each Route Plan Stop snapshots its Supplier's GPS at the time it's added
(never a live read) so a later correction to a Supplier's pin never silently
rewrites a dispatched plan's own historical route. Initial sequencing comes
from `optimize_route` (Google picks the order); `resequence_route_stops`
lets staff manually override that order afterward, recomputing legs for the
now-fixed order rather than leaving totals stale.

Dispatching a plan doesn't replace Inward Truck's own gate/unloading/
put-away flow -- same as Pickup Run, it creates one Inward Truck per line
(via `inward_api.add_truck`), tagged via Inward Truck's new `route_plan`
field. Overload (total weight over the chosen vehicle's capacity_tonnes) is
a soft warning returned alongside the dispatch response, not a hard block --
BRD says "overload warnings", not "overload prevention".
"""

import frappe
from frappe import _

from dms_erp.catalog.utils import item_weight_per_box_kg
from dms_erp.pagination import clamp
from dms_erp.purchase import routing_client
from dms_erp.purchase.po_api import remaining_ready_qty_for_line
from dms_erp.warehouse import inward_api

ROUTE_PLAN_WRITE_ROLES = {"DMS Purchase", "DMS Warehouse", "DMS Management", "System Manager"}
ROUTE_PLAN_TRANSITIONS = {
	"Draft": {"Dispatched", "Cancelled"},
	"Dispatched": {"Completed"},
}


def _assert_can_manage_route_plans():
	if not set(frappe.get_roles(frappe.session.user)) & ROUTE_PLAN_WRITE_ROLES:
		frappe.throw(_("Only Purchase, Warehouse, or Management can manage route plans."), frappe.PermissionError)


def _resolve_vehicle_row(transporter: str, transporter_vehicle: str):
	t = frappe.get_doc("Transporter", transporter)
	row = next((r for r in t.vehicles if r.name == transporter_vehicle), None)
	if not row:
		frappe.throw(_("Vehicle {0} not found on transporter {1}.").format(transporter_vehicle, transporter), frappe.ValidationError)
	return row


def _route_origin() -> tuple[float, float]:
	settings = frappe.get_single("DMS Purchase Settings")
	if settings.route_origin_latitude is None or settings.route_origin_longitude is None:
		frappe.throw(
			_("The route origin (warehouse GPS) isn't configured yet — set it on DMS Purchase Settings."),
			frappe.ValidationError,
		)
	return (settings.route_origin_latitude, settings.route_origin_longitude)


def _snapshot_stop(supplier: str, sequence_no: int) -> dict:
	lat, lng = frappe.db.get_value("Supplier", supplier, ["custom_latitude", "custom_longitude"]) or (None, None)
	if lat is None or lng is None:
		frappe.throw(_("Supplier {0} has no GPS coordinates set — required for route planning.").format(supplier), frappe.ValidationError)
	return {
		"sequence_no": sequence_no,
		"supplier": supplier,
		"latitude": lat,
		"longitude": lng,
		"leg_distance_km": None,
		"leg_duration_minutes": None,
	}


def _build_stops(stops: list[dict]) -> list[dict]:
	if not stops:
		frappe.throw(_("A Route Plan needs at least one stop."), frappe.ValidationError)
	suppliers = [s["supplier"] for s in stops]
	if len(suppliers) != len(set(suppliers)):
		frappe.throw(_("Each supplier can only appear once in a Route Plan's stops."), frappe.ValidationError)
	return [_snapshot_stop(supplier, idx + 1) for idx, supplier in enumerate(suppliers)]


def _validate_lines(stop_suppliers: set[str], lines: list[dict], exclude_route_plan: str | None = None) -> tuple[float, list[dict]]:
	"""Mirrors pickup_run_api._validate_lines's per-PO-line ready-qty check,
	generalized from "must match the run's one supplier" to "must match one of
	this plan's stops" — a Route Plan has no single-supplier constraint. Returns
	(total_weight_kg, enriched lines) rather than a box count, since capacity
	here is tonnage-based (the chosen Transporter Vehicle's capacity_tonnes),
	not box count."""
	for line in lines:
		if not line.get("purchase_order_item"):
			frappe.throw(
				_("Each route plan line needs a purchase_order_item (the specific Purchase Order line, not the Purchase Order's own name)."),
				frappe.ValidationError,
			)
		if not line.get("qty"):
			frappe.throw(_("Each route plan line needs a qty."), frappe.ValidationError)

	# Same double-booking-within-one-request defense as pickup_run_api: sum
	# requested qty per PO line item first, check each unique line once against
	# that combined total.
	requested_by_po_item: dict[str, int] = {}
	for line in lines:
		po_item_name = line["purchase_order_item"]
		requested_by_po_item[po_item_name] = requested_by_po_item.get(po_item_name, 0) + int(line["qty"])

	enriched = []
	total_weight_kg = 0.0
	checked_po_items: set[str] = set()
	for line in lines:
		po_item_name = line["purchase_order_item"]
		po_item = frappe.get_doc("Purchase Order Item", po_item_name)
		po_supplier = frappe.db.get_value("Purchase Order", po_item.parent, "supplier")
		if po_supplier not in stop_suppliers:
			frappe.throw(
				_("Purchase Order Item {0} belongs to supplier {1}, which has no stop on this route.").format(po_item_name, po_supplier),
				frappe.ValidationError,
			)

		qty = int(line["qty"])
		if po_item_name not in checked_po_items:
			checked_po_items.add(po_item_name)
			remaining = remaining_ready_qty_for_line(po_item_name, exclude_route_plan=exclude_route_plan)
			requested = requested_by_po_item[po_item_name]
			if requested > remaining:
				frappe.throw(
					_("Only {0} boxes of {1} are still available for pickup on line {2} (already booked or reserved elsewhere).").format(
						remaining, po_item.item_code, po_item_name
					),
					frappe.ValidationError,
				)

		weight_per_box = item_weight_per_box_kg(po_item.item_code) or 0
		total_weight_kg += weight_per_box * qty
		enriched.append(
			{
				"supplier": po_supplier,
				"purchase_order": po_item.parent,
				"purchase_order_item": po_item_name,
				"item": po_item.item_code,
				"qty": qty,
			}
		)

	return total_weight_kg, enriched


def _serialize_line(row) -> dict:
	weight_per_box_kg = item_weight_per_box_kg(row.item)
	return {
		"supplier": row.supplier,
		"purchaseOrder": row.purchase_order,
		"purchaseOrderItem": row.purchase_order_item,
		"item": row.item,
		"qty": row.qty,
		"weightPerBoxKg": weight_per_box_kg,
		"totalWeightKg": (weight_per_box_kg or 0) * row.qty if weight_per_box_kg is not None else None,
	}


def _serialize_stop(row) -> dict:
	return {
		"id": row.name,
		"sequenceNo": row.sequence_no,
		"supplier": row.supplier,
		"latitude": row.latitude,
		"longitude": row.longitude,
		"legDistanceKm": row.leg_distance_km,
		"legDurationMinutes": row.leg_duration_minutes,
	}


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"transporter": doc.transporter,
		"transporterVehicle": doc.transporter_vehicle,
		"vehicleKind": doc.vehicle_kind,
		"scheduledDate": doc.scheduled_date,
		"status": doc.status,
		"totalDistanceKm": doc.total_distance_km,
		"totalDurationMinutes": doc.total_duration_minutes,
		"totalWeightKg": doc.total_weight_kg,
		"stops": [_serialize_stop(row) for row in sorted(doc.stops, key=lambda r: r.sequence_no)],
		"lines": [_serialize_line(row) for row in doc.lines],
	}


@frappe.whitelist(methods=["GET"])
def list_route_plans(status: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = {}
	if status:
		filters["status"] = status
	if search:
		filters["name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Route Plan", filters=filters)
	names = frappe.get_all("Route Plan", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit)
	return {
		"items": [_serialize(frappe.get_doc("Route Plan", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_route_plan(route_plan: str):
	return _serialize(frappe.get_doc("Route Plan", route_plan))


@frappe.whitelist(methods=["POST"])
def create_route_plan(transporter: str, transporter_vehicle: str, stops: list[dict], lines: list[dict], scheduled_date=None):
	_assert_can_manage_route_plans()
	return _create_route_plan(transporter, transporter_vehicle, stops, lines, scheduled_date)


def _create_route_plan(transporter: str, transporter_vehicle: str, stops: list[dict], lines: list[dict], scheduled_date=None) -> dict:
	if not lines:
		frappe.throw(_("A Route Plan needs at least one line."), frappe.ValidationError)

	vehicle_row = _resolve_vehicle_row(transporter, transporter_vehicle)
	built_stops = _build_stops(stops)
	stop_suppliers = {s["supplier"] for s in built_stops}
	total_weight_kg, enriched_lines = _validate_lines(stop_suppliers, lines)

	doc = frappe.get_doc(
		{
			"doctype": "Route Plan",
			"transporter": transporter,
			"transporter_vehicle": transporter_vehicle,
			"vehicle_kind": vehicle_row.vehicle_type,
			"scheduled_date": scheduled_date,
			"status": "Draft",
			"total_weight_kg": total_weight_kg,
			"total_distance_km": 0,
			"total_duration_minutes": 0,
			"stops": built_stops,
			"lines": enriched_lines,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def add_route_plan_stop(route_plan: str, supplier: str):
	_assert_can_manage_route_plans()
	return _add_route_plan_stop(route_plan, supplier)


def _add_route_plan_stop(route_plan: str, supplier: str) -> dict:
	doc = frappe.get_doc("Route Plan", route_plan)
	if doc.status != "Draft":
		frappe.throw(_("Can only add stops to a Draft Route Plan."), frappe.ValidationError)
	if supplier in {row.supplier for row in doc.stops}:
		frappe.throw(_("{0} already has a stop on this route.").format(supplier), frappe.ValidationError)

	doc.append("stops", _snapshot_stop(supplier, len(doc.stops) + 1))
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def add_route_plan_line(route_plan: str, purchase_order_item: str, qty: int):
	_assert_can_manage_route_plans()
	return _add_route_plan_line(route_plan, purchase_order_item, qty)


def _add_route_plan_line(route_plan: str, purchase_order_item: str, qty: int) -> dict:
	doc = frappe.get_doc("Route Plan", route_plan)
	if doc.status != "Draft":
		frappe.throw(_("Can only add lines to a Draft Route Plan."), frappe.ValidationError)

	stop_suppliers = {row.supplier for row in doc.stops}
	proposed = [{"purchase_order_item": row.purchase_order_item, "qty": row.qty} for row in doc.lines]
	proposed.append({"purchase_order_item": purchase_order_item, "qty": qty})

	total_weight_kg, enriched = _validate_lines(stop_suppliers, proposed, exclude_route_plan=doc.name)

	doc.set("lines", enriched)
	doc.total_weight_kg = total_weight_kg
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def optimize_route(route_plan: str):
	"""Calls routing_client.optimize_waypoints (lets Google choose the stop
	order) for a Draft plan's current stops, overwriting their
	sequence_no/leg_distance_km/leg_duration_minutes and the parent's total_*
	fields. Raises routing_client.RouteOptimizationFailed on any Maps-API
	failure -- this is a synchronous staff button-click, unlike
	comms/whats91.py's fire-and-log OTP pattern, so the failure must surface."""
	_assert_can_manage_route_plans()
	return _optimize_route(route_plan)


def _optimize_route(route_plan: str) -> dict:
	doc = frappe.get_doc("Route Plan", route_plan)
	if doc.status != "Draft":
		frappe.throw(_("Can only optimize a Draft Route Plan."), frappe.ValidationError)
	if not doc.stops:
		frappe.throw(_("Route Plan has no stops to optimize."), frappe.ValidationError)

	origin = _route_origin()
	stops = sorted(doc.stops, key=lambda r: r.sequence_no)
	result = routing_client.optimize_waypoints(origin, [(s.latitude, s.longitude) for s in stops])

	new_stops = []
	for position, original_index in enumerate(result["order"]):
		original = stops[original_index]
		leg = result["legs"][position]
		new_stops.append(
			{
				"sequence_no": position + 1,
				"supplier": original.supplier,
				"latitude": original.latitude,
				"longitude": original.longitude,
				"leg_distance_km": leg["distance_km"],
				"leg_duration_minutes": leg["duration_minutes"],
			}
		)

	doc.set("stops", new_stops)
	doc.total_distance_km = sum(s["leg_distance_km"] for s in new_stops)
	doc.total_duration_minutes = sum(s["leg_duration_minutes"] for s in new_stops)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def resequence_route_stops(route_plan: str, stop_order: list[str]):
	"""`stop_order` is the staff-chosen ordering, given as the ordered list of
	Route Plan Stop row names (same "row name as reference" convention used
	elsewhere in this app). Recomputes legs via
	routing_client.compute_fixed_order_legs — the Directions API for a
	caller-FIXED order — rather than re-optimizing, since the order is now
	staff's to keep, not Google's to re-decide."""
	_assert_can_manage_route_plans()
	return _resequence_route_stops(route_plan, stop_order)


def _resequence_route_stops(route_plan: str, stop_order: list[str]) -> dict:
	doc = frappe.get_doc("Route Plan", route_plan)
	if doc.status != "Draft":
		frappe.throw(_("Can only resequence a Draft Route Plan."), frappe.ValidationError)

	rows_by_name = {row.name: row for row in doc.stops}
	if set(stop_order) != set(rows_by_name):
		frappe.throw(_("stop_order must list every existing stop exactly once."), frappe.ValidationError)

	origin = _route_origin()
	ordered_rows = [rows_by_name[name] for name in stop_order]
	legs = routing_client.compute_fixed_order_legs(origin, [(r.latitude, r.longitude) for r in ordered_rows])

	new_stops = []
	for position, (row, leg) in enumerate(zip(ordered_rows, legs)):
		new_stops.append(
			{
				"sequence_no": position + 1,
				"supplier": row.supplier,
				"latitude": row.latitude,
				"longitude": row.longitude,
				"leg_distance_km": leg["distance_km"],
				"leg_duration_minutes": leg["duration_minutes"],
			}
		)

	doc.set("stops", new_stops)
	doc.total_distance_km = sum(s["leg_distance_km"] for s in new_stops)
	doc.total_duration_minutes = sum(s["leg_duration_minutes"] for s in new_stops)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def advance_route_plan_status(route_plan: str, next_status: str):
	_assert_can_manage_route_plans()
	return _advance_route_plan_status(route_plan, next_status)


def _advance_route_plan_status(route_plan: str, next_status: str) -> dict:
	doc = frappe.get_doc("Route Plan", route_plan)
	allowed = ROUTE_PLAN_TRANSITIONS.get(doc.status, set())
	if next_status not in allowed:
		frappe.throw(_("Cannot move a {0} Route Plan to {1}.").format(doc.status, next_status), frappe.ValidationError)

	overload_warning = None
	if next_status == "Dispatched":
		# Re-validate right before committing to real Inward Trucks — something
		# else may have consumed the same ready stock since this plan was drafted.
		stop_suppliers = {row.supplier for row in doc.stops}
		proposed = [{"purchase_order_item": row.purchase_order_item, "qty": row.qty} for row in doc.lines]
		total_weight_kg, _enriched = _validate_lines(stop_suppliers, proposed, exclude_route_plan=doc.name)

		vehicle_row = _resolve_vehicle_row(doc.transporter, doc.transporter_vehicle)
		capacity_kg = (vehicle_row.capacity_tonnes or 0) * 1000
		if capacity_kg and total_weight_kg > capacity_kg:
			# BRD C.5 asks for an overload WARNING, not prevention — dispatch
			# still proceeds, flagged for the planner to review.
			overload_warning = _(
				"This route totals {0} kg, over {1}'s {2} kg capacity — dispatched anyway, review before the truck leaves."
			).format(round(total_weight_kg), vehicle_row.vehicle_number, round(capacity_kg))

		for row in doc.lines:
			inward_api.add_truck(
				supplier=row.supplier,
				item=row.item,
				boxes=row.qty,
				vehicle_number=vehicle_row.vehicle_number,
				purchase_order=row.purchase_order,
				purchase_order_item=row.purchase_order_item,
				route_plan=doc.name,
			)

	doc.status = next_status
	doc.save(ignore_permissions=True)
	result = _serialize(doc)
	if overload_warning:
		result["overloadWarning"] = overload_warning
	return result
