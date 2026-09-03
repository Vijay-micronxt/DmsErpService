"""Purchase Orders (BRD §13.1) — native ERPNext Purchase Order / Purchase Order Item,
submitted immediately on creation (the frontend has no separate draft/approval step —
"Raise PO" is one action). `readyQty` (supplier-confirmed readiness, §13.2) has no
ERPNext equivalent and is a Custom Field; `receivedQty` is ERPNext's own native
`received_qty` on the PO Item, kept accurate by warehouse/allocation_api.py linking
Purchase Receipts back to the PO line it fulfills.
"""

import frappe
from frappe import _
from frappe.utils import getdate, today

from dms_erp.warehouse.utils import default_company

PURCHASE_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}


def _assert_can_manage_purchase():
	if not set(frappe.get_roles(frappe.session.user)) & PURCHASE_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage purchase orders."), frappe.PermissionError)


def _serialize_line(row) -> dict:
	item = frappe.get_cached_doc("Item", row.item_code)
	return {
		"id": row.name,
		"itemCode": row.item_code,
		"itemName": item.item_name,
		"orderedQty": row.qty,
		"readyQty": row.custom_ready_qty or 0,
		"receivedQty": row.received_qty or 0,
	}


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"number": doc.name,
		"date": doc.transaction_date,
		"supplier": doc.supplier,
		"expectedReadyDate": doc.schedule_date,
		"remarks": doc.custom_remarks,
		"sourceInquiry": doc.custom_source_inquiry,
		"lines": [_serialize_line(row) for row in doc.items],
	}


@frappe.whitelist(methods=["GET"])
def list_purchase_orders():
	names = frappe.get_all("Purchase Order", pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Purchase Order", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def get_purchase_order(po: str):
	return _serialize(frappe.get_doc("Purchase Order", po))


@frappe.whitelist(methods=["POST"])
def create_purchase_order(
	item: str,
	ordered_qty: float,
	supplier: str,
	expected_ready_date,
	remarks: str | None = None,
	source_inquiry: str | None = None,
):
	_assert_can_manage_purchase()

	po = frappe.get_doc(
		{
			"doctype": "Purchase Order",
			"supplier": supplier,
			"company": default_company(),
			"transaction_date": today(),
			"schedule_date": expected_ready_date,
			"custom_remarks": remarks,
			"custom_source_inquiry": source_inquiry,
			"items": [{"item_code": item, "qty": ordered_qty, "schedule_date": expected_ready_date}],
		}
	)
	po.insert(ignore_permissions=True)
	po.submit()
	return _serialize(po)


@frappe.whitelist(methods=["POST", "PUT"])
def set_line_ready(po: str, line: str, ready_qty: float):
	_assert_can_manage_purchase()

	doc = frappe.get_doc("Purchase Order", po)
	row = next((r for r in doc.items if r.name == line), None)
	if not row:
		frappe.throw(_("No such line on this Purchase Order."), frappe.DoesNotExistError)

	row.custom_ready_qty = max(0, min(float(ready_qty), row.qty))
	doc.save(ignore_permissions=True)
	return _serialize_line(row)


@frappe.whitelist(methods=["GET"])
def line_progress(line: str):
	row = frappe.get_doc("Purchase Order Item", line)
	planned_qty = frappe.db.sql(
		"select coalesce(sum(boxes), 0) from `tabInward Truck` where purchase_order_item=%s", (line,)
	)[0][0]
	received_qty = row.received_qty or 0
	ready_qty = row.custom_ready_qty or 0
	remaining_to_plan = max(0, ready_qty - planned_qty)

	if planned_qty >= row.qty:
		status = "Fully planned"
	elif planned_qty > 0:
		status = "Partially planned"
	elif ready_qty > 0:
		status = "Ready to plan"
	else:
		status = "Awaiting readiness"

	return {
		"plannedQty": planned_qty,
		"receivedQty": received_qty,
		"remainingToPlan": remaining_to_plan,
		"status": status,
	}


def list_pending_po_lines() -> list[dict]:
	"""Every submitted PO line not yet fully received, with how many days past its
	own expected-ready date it's now sitting (0 if not yet due, or not yet set).
	Line-level rather than summed-per-PO, so a partially-received line inside an
	otherwise-complete-looking PO still surfaces. Shared by the Phase 8 purchase
	dashboard's pendingPOs/supplierDelays counts and the Phase 17 PO Pending Report."""
	rows = frappe.db.sql(
		"""
		select poi.name as line, poi.parent as po, po.supplier as supplier,
			po.schedule_date as expected_ready_date, poi.item_code as item_code,
			poi.qty as ordered_qty, coalesce(poi.received_qty, 0) as received_qty
		from `tabPurchase Order Item` poi
		inner join `tabPurchase Order` po on po.name = poi.parent
		where po.docstatus = 1
		""",
		as_dict=True,
	)
	today_date = getdate(today())
	out = []
	for row in rows:
		pending_qty = row.ordered_qty - row.received_qty
		if pending_qty <= 0:
			continue
		is_overdue = bool(row.expected_ready_date) and getdate(row.expected_ready_date) < today_date
		out.append(
			{
				"line": row.line,
				"po": row.po,
				"supplier": row.supplier,
				"itemCode": row.item_code,
				"orderedQty": row.ordered_qty,
				"receivedQty": row.received_qty,
				"pendingQty": pending_qty,
				"expectedReadyDate": row.expected_ready_date,
				"daysOverdue": (today_date - getdate(row.expected_ready_date)).days if is_overdue else 0,
			}
		)
	return out


def remaining_ready_qty_for_line(po_item: str, ready_qty: float | None = None, exclude_pickup_run: str | None = None) -> float:
	"""How many boxes of this PO line's supplier-confirmed-ready qty are still
	available to book onto a truck. Nets out two things: boxes already booked
	onto an Inward Truck (this covers both a directly-booked truck and one
	created by dispatching a Pickup Run, since dispatch creates real Inward
	Truck rows), and boxes reserved by any other still-open (Draft) Pickup Run
	line for this same PO line — a Draft run has no Inward Truck yet, so
	without this second check two draft runs could both claim the same ready
	stock. A Dispatched/Completed/Cancelled Pickup Run's own lines are never
	counted here (Dispatched is already covered via its Inward Trucks;
	Completed/Cancelled reserve nothing). `exclude_pickup_run` lets a run
	re-validate its own edited lines without double-counting its own
	not-yet-saved reservation.

	When `ready_qty` isn't supplied (the pickup_run_api validation path, not
	the read-only listing below which always passes it in), this locks the
	Purchase Order Item row (`for_update=True`) for the rest of the caller's
	transaction -- without it, two concurrent requests booking the same line
	can both read the same booked/reserved sums before either commits its own
	reservation, and both pass validation, over-booking the line."""
	if ready_qty is None:
		ready_qty = frappe.db.get_value("Purchase Order Item", po_item, "custom_ready_qty", for_update=True) or 0

	booked = frappe.db.sql("select coalesce(sum(boxes), 0) from `tabInward Truck` where purchase_order_item=%s", (po_item,))[0][0]

	reserved = frappe.db.sql(
		"""
		select coalesce(sum(pri.qty), 0)
		from `tabPickup Run Item` pri
		inner join `tabPickup Run` pr on pr.name = pri.parent
		where pri.purchase_order_item = %s and pr.status = 'Draft' and pr.name != %s
		""",
		(po_item, exclude_pickup_run or ""),
	)[0][0]

	return ready_qty - booked - reserved


def list_materials_ready_for_pickup() -> list[dict]:
	"""PO lines the supplier has confirmed ready (custom_ready_qty > 0) but that
	haven't all been booked onto an Inward Truck or reserved by an open Pickup
	Run yet. Shared by the Phase 8 purchase dashboard's materialsReadyForPickup,
	the Phase 16 Purchase Pickup Plan report, and Pickup Run's own capacity
	validation (remaining_ready_qty_for_line, above — same underlying math)."""
	out = []
	for row in frappe.get_all(
		"Purchase Order Item",
		filters={"custom_ready_qty": [">", 0], "docstatus": 1},
		fields=["name", "parent as po", "item_code", "custom_ready_qty as ready_qty"],
	):
		remaining = remaining_ready_qty_for_line(row.name, ready_qty=row.ready_qty)
		if remaining > 0:
			out.append(
				{
					"line": row.name,
					"po": row.po,
					"supplier": frappe.db.get_value("Purchase Order", row.po, "supplier"),
					"itemCode": row.item_code,
					"readyQty": remaining,
				}
			)
	return out
