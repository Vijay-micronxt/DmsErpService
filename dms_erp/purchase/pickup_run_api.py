"""Pickup Run — groups supplier-confirmed-ready PO lines onto one truck, with
capacity checked against a Vehicle Type before booking. No GPS/address/route
calculation and no multi-supplier runs are modeled — a run is scoped to a
single supplier, and the truck itself stays a free-text plate number
(`vehicle_number`) same as Inward Truck already does; only the *type* of
vehicle (and its box capacity) is a real linked master.

Dispatching a run doesn't replace Inward Truck's own gate/unloading/put-away
flow — it creates one Inward Truck per line (via the same `inward_api.add_truck`
every direct truck booking already goes through), tagged back to the run via
Inward Truck's new `pickup_run` field. Everything downstream of that point
(list_materials_ready_for_pickup's netting, the gate queue, put-away) is
unaffected and untouched.
"""

import frappe
from frappe import _

from dms_erp.purchase.po_api import remaining_ready_qty_for_line
from dms_erp.warehouse import inward_api

PICKUP_RUN_WRITE_ROLES = {"DMS Purchase", "DMS Warehouse", "DMS Management", "System Manager"}
PICKUP_RUN_TRANSITIONS = {
	"Draft": {"Dispatched", "Cancelled"},
	"Dispatched": {"Completed"},
}


def _assert_can_manage_pickup_runs():
	if not set(frappe.get_roles(frappe.session.user)) & PICKUP_RUN_WRITE_ROLES:
		frappe.throw(_("Only Purchase, Warehouse, or Management can manage pickup runs."), frappe.PermissionError)


def _serialize_vehicle_type(doc) -> dict:
	return {"id": doc.name, "name": doc.vehicle_type_name, "capacityBoxes": doc.capacity_boxes}


def _serialize_line(row) -> dict:
	return {
		"purchaseOrder": row.purchase_order,
		"purchaseOrderItem": row.purchase_order_item,
		"item": row.item,
		"qty": row.qty,
	}


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"supplier": doc.supplier,
		"vehicleType": doc.vehicle_type,
		"vehicleNumber": doc.vehicle_number,
		"scheduledDate": doc.scheduled_date,
		"status": doc.status,
		"totalBoxes": doc.total_boxes,
		"lines": [_serialize_line(row) for row in doc.lines],
	}


def _validate_lines(supplier: str, vehicle_type: str, lines: list[dict], exclude_pickup_run: str | None = None) -> tuple[int, list[dict]]:
	"""Validates and enriches a proposed set of {purchase_order_item, qty} lines
	against both checks: the PO line's own remaining ready-for-pickup qty (per
	line, netting out other Draft runs' reservations for that same line), and
	the vehicle type's total box capacity (across all lines together). Returns
	(total_boxes, enriched lines with po/item filled in) for the caller to
	build/overwrite the doc's child table from. `exclude_pickup_run` should be
	the run's own name when re-validating an existing run's lines, so its own
	already-saved reservation isn't double-counted against itself."""
	vt = frappe.get_doc("Vehicle Type", vehicle_type)

	enriched = []
	total_boxes = 0
	for line in lines:
		po_item_name = line["purchase_order_item"]
		po_item = frappe.get_doc("Purchase Order Item", po_item_name)
		po_supplier = frappe.db.get_value("Purchase Order", po_item.parent, "supplier")
		if po_supplier != supplier:
			frappe.throw(
				_("Purchase Order Item {0} belongs to supplier {1}, not {2} — a Pickup Run can only cover one supplier.").format(
					po_item_name, po_supplier, supplier
				),
				frappe.ValidationError,
			)

		qty = int(line["qty"])
		remaining = remaining_ready_qty_for_line(po_item_name, exclude_pickup_run=exclude_pickup_run)
		if qty > remaining:
			frappe.throw(
				_("Only {0} boxes of {1} are still available for pickup on line {2} (already booked or reserved elsewhere).").format(
					remaining, po_item.item_code, po_item_name
				),
				frappe.ValidationError,
			)

		total_boxes += qty
		enriched.append({"purchase_order": po_item.parent, "purchase_order_item": po_item_name, "item": po_item.item_code, "qty": qty})

	if total_boxes > vt.capacity_boxes:
		frappe.throw(
			_("{0} has only {1} boxes of capacity — this run totals {2} boxes.").format(vt.vehicle_type_name, vt.capacity_boxes, total_boxes),
			frappe.ValidationError,
		)

	return total_boxes, enriched


@frappe.whitelist(methods=["GET"])
def list_vehicle_types():
	names = frappe.get_all("Vehicle Type", pluck="name", order_by="vehicle_type_name")
	return [_serialize_vehicle_type(frappe.get_doc("Vehicle Type", name)) for name in names]


@frappe.whitelist(methods=["POST"])
def create_vehicle_type(name: str, capacity_boxes: int):
	_assert_can_manage_pickup_runs()

	doc = frappe.get_doc({"doctype": "Vehicle Type", "vehicle_type_name": name, "capacity_boxes": capacity_boxes})
	doc.insert(ignore_permissions=True)
	return _serialize_vehicle_type(doc)


@frappe.whitelist(methods=["GET"])
def list_pickup_runs(supplier: str | None = None, status: str | None = None):
	filters = {}
	if supplier:
		filters["supplier"] = supplier
	if status:
		filters["status"] = status
	names = frappe.get_all("Pickup Run", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Pickup Run", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def get_pickup_run(pickup_run: str):
	return _serialize(frappe.get_doc("Pickup Run", pickup_run))


@frappe.whitelist(methods=["POST"])
def create_pickup_run(supplier: str, vehicle_type: str, lines: list[dict], vehicle_number: str | None = None, scheduled_date=None):
	_assert_can_manage_pickup_runs()

	if not lines:
		frappe.throw(_("A Pickup Run needs at least one line."), frappe.ValidationError)

	total_boxes, enriched = _validate_lines(supplier, vehicle_type, lines)

	doc = frappe.get_doc(
		{
			"doctype": "Pickup Run",
			"supplier": supplier,
			"vehicle_type": vehicle_type,
			"vehicle_number": vehicle_number,
			"scheduled_date": scheduled_date,
			"status": "Draft",
			"total_boxes": total_boxes,
			"lines": enriched,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def add_pickup_run_line(pickup_run: str, purchase_order_item: str, qty: int):
	_assert_can_manage_pickup_runs()

	doc = frappe.get_doc("Pickup Run", pickup_run)
	if doc.status != "Draft":
		frappe.throw(_("Can only add lines to a Draft Pickup Run."), frappe.ValidationError)

	proposed = [{"purchase_order_item": row.purchase_order_item, "qty": row.qty} for row in doc.lines]
	proposed.append({"purchase_order_item": purchase_order_item, "qty": qty})

	total_boxes, enriched = _validate_lines(doc.supplier, doc.vehicle_type, proposed, exclude_pickup_run=doc.name)

	doc.set("lines", enriched)
	doc.total_boxes = total_boxes
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def advance_pickup_run_status(pickup_run: str, next_status: str):
	_assert_can_manage_pickup_runs()

	doc = frappe.get_doc("Pickup Run", pickup_run)
	allowed = PICKUP_RUN_TRANSITIONS.get(doc.status, set())
	if next_status not in allowed:
		frappe.throw(_("Cannot move a {0} Pickup Run to {1}.").format(doc.status, next_status), frappe.ValidationError)

	if next_status == "Dispatched":
		# Re-validate right before committing to real Inward Trucks — something
		# else may have consumed the same ready stock since this run was drafted.
		proposed = [{"purchase_order_item": row.purchase_order_item, "qty": row.qty} for row in doc.lines]
		_validate_lines(doc.supplier, doc.vehicle_type, proposed, exclude_pickup_run=doc.name)

		for row in doc.lines:
			inward_api.add_truck(
				supplier=doc.supplier,
				item=row.item,
				boxes=row.qty,
				vehicle_number=doc.vehicle_number,
				purchase_order=row.purchase_order,
				purchase_order_item=row.purchase_order_item,
				pickup_run=doc.name,
			)

	doc.status = next_status
	doc.save(ignore_permissions=True)
	return _serialize(doc)
