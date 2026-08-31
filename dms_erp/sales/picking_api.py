"""Picking — a genuine custom doctype (Pick Task). ERPNext's native Pick List exists
but is a coarser, all-or-nothing document per pick run; Pacific wants one row per
order line with a suggested bay, a partially-allocatable qty, a named picker and a
simple Pending/Allocated/Picked status, which Pick List doesn't model at that
granularity. Auto-allocation reads live stock from warehouse/utils.py — nothing here
owns its own view of what's on hand.

Marking a task Picked does not itself create a Delivery Note or reduce stock (see
sales/order_api.py's module docstring) — that's a natural future refinement.
"""

import frappe
from frappe import _

from dms_erp.warehouse.utils import available_for_item, suggest_bays

PICKING_WRITE_ROLES = {"DMS Warehouse", "DMS Management", "System Manager"}


def _assert_can_manage_picking():
	if not set(frappe.get_roles(frappe.session.user)) & PICKING_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage picking."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"orderNumber": doc.sales_order,
		"itemCode": doc.item,
		"batchNumber": doc.batch_no,
		"qty": doc.qty,
		"allocated": doc.allocated_qty,
		"suggestedBayId": doc.suggested_bay,
		"picker": doc.picker,
		"status": doc.status,
	}


@frappe.whitelist(methods=["GET"])
def list_pick_tasks(order: str | None = None):
	filters = {"sales_order": order} if order else {}
	names = frappe.get_all("Pick Task", filters=filters, pluck="name", order_by="creation asc")
	return [_serialize(frappe.get_doc("Pick Task", name)) for name in names]


def ensure_pick_tasks(order: str):
	"""Create one Pick Task per Sales Order line, skipping lines that already have
	one. Called when an order enters the Picking stage (see order_api.advance_order_stage)."""
	so = frappe.get_doc("Sales Order", order)
	existing = set(frappe.get_all("Pick Task", filters={"sales_order": order}, pluck="sales_order_item"))

	created = []
	for row in so.items:
		if row.name in existing:
			continue
		item_group = frappe.get_cached_value("Item", row.item_code, "item_group")
		suggestion = suggest_bays(item_group, row.qty)["main"]
		suggested_bay = suggestion[0]["bay"]["id"] if suggestion else None

		doc = frappe.get_doc(
			{
				"doctype": "Pick Task",
				"sales_order": order,
				"sales_order_item": row.name,
				"item": row.item_code,
				"qty": row.qty,
				"suggested_bay": suggested_bay,
				"status": "Pending",
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(_serialize(doc))
	return created


@frappe.whitelist(methods=["POST"])
def auto_allocate(task: str):
	_assert_can_manage_picking()

	doc = frappe.get_doc("Pick Task", task)
	available = available_for_item(doc.item)
	allocated = min(doc.qty, available)

	doc.allocated_qty = allocated
	doc.status = "Allocated" if allocated > 0 else "Pending"
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def patch_task(task: str, patch: dict):
	_assert_can_manage_picking()

	field_map = {"picker": "picker", "status": "status", "allocated": "allocated_qty", "batchNumber": "batch_no", "suggestedBayId": "suggested_bay"}

	doc = frappe.get_doc("Pick Task", task)
	for key, value in patch.items():
		fieldname = field_map.get(key)
		if fieldname:
			doc.set(fieldname, value)
	doc.save(ignore_permissions=True)
	return _serialize(doc)
