"""Inward truck / gate queue (BRD §14). No ERPNext doctype models "a truck en route
with an LR number working through Scheduled → At Gate → Unloading → Put-away" — the
actual stock effect happens later, when Bay Allocation confirms and posts a Purchase
Receipt (see allocation_api.create_allocation).
"""

import frappe
from frappe import _

INWARD_WRITE_ROLES = {"DMS Warehouse", "DMS Purchase", "DMS Management", "System Manager"}
TRUCK_FLOW = ["Scheduled", "At Gate", "Unloading", "Put-away"]


def _assert_can_manage_inward():
	if not set(frappe.get_roles(frappe.session.user)) & INWARD_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Purchase can manage inward trucks."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"lr": doc.lr_number,
		"supplier": doc.supplier,
		"vehicle": doc.vehicle_number,
		"eta": doc.eta,
		"boxes": doc.boxes,
		"status": doc.status,
		"item": doc.item,
		"batchNumber": doc.batch_no,
		"poReference": doc.po_reference,
		"poId": doc.purchase_order,
		"poLineId": doc.purchase_order_item,
		"allocationSlip": doc.allocation_slip,
		"pickupRun": doc.pickup_run,
	}


@frappe.whitelist(methods=["GET"])
def list_trucks():
	names = frappe.get_all("Inward Truck", pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Inward Truck", name)) for name in names]


@frappe.whitelist(methods=["POST"])
def add_truck(
	supplier: str,
	item: str,
	boxes: int,
	lr_number: str | None = None,
	vehicle_number: str | None = None,
	eta=None,
	purchase_order: str | None = None,
	purchase_order_item: str | None = None,
	po_reference: str | None = None,
	pickup_run: str | None = None,
):
	_assert_can_manage_inward()

	truck = frappe.get_doc(
		{
			"doctype": "Inward Truck",
			"lr_number": lr_number or f"LR-PENDING-{frappe.generate_hash(length=6).upper()}",
			"supplier": supplier,
			"vehicle_number": vehicle_number,
			"eta": eta,
			"boxes": boxes,
			"item": item,
			"status": "Scheduled",
			"purchase_order": purchase_order,
			"purchase_order_item": purchase_order_item,
			"po_reference": po_reference,
			"pickup_run": pickup_run,
		}
	)
	truck.insert(ignore_permissions=True)
	return _serialize(truck)


@frappe.whitelist(methods=["POST", "PUT"])
def advance_truck(truck: str, next_status: str):
	_assert_can_manage_inward()

	if next_status not in TRUCK_FLOW:
		frappe.throw(_("Invalid status: {0}").format(next_status), frappe.ValidationError)

	doc = frappe.get_doc("Inward Truck", truck)
	doc.status = next_status
	doc.save(ignore_permissions=True)
	return _serialize(doc)
