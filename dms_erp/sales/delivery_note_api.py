"""Delivery Notes for the staff app: list + detail.

Notes are posted automatically at Dispatched (order_api._create_delivery_note) -- this is
the read side, so staff can find a dispatch, see who carried it and which order it served,
without going into the ERPNext desk. Read-only on purpose: a Delivery Note is the stock
movement itself, never edited or created by hand here.
"""

import frappe
from frappe import _

from dms_erp.catalog.utils import item_images, item_weight_per_box_kg
from dms_erp.pagination import clamp

DELIVERY_NOTE_READ_ROLES = {"DMS Sales", "DMS Warehouse", "DMS Management", "DMS Finance", "System Manager"}
# docstatus 1 is the normal case; 2 is a cancelled note, kept visible so a reversed dispatch
# is not mistaken for one that never happened.
STATUSES = {"Submitted": 1, "Cancelled": 2}
BILLING = ("Not invoiced", "Partly invoiced", "Fully invoiced")

_SEARCH_FIELDS = ["name", "customer", "customer_name", "vehicle_no", "lr_no", "transporter_name"]


def _assert_can_read():
	if not set(frappe.get_roles(frappe.session.user)) & DELIVERY_NOTE_READ_ROLES:
		frappe.throw(_("You don't have access to Delivery Notes."), frappe.PermissionError)


def _billing_label(per_billed) -> str:
	per_billed = per_billed or 0
	if per_billed >= 99.99:
		return "Fully invoiced"
	return "Partly invoiced" if per_billed > 0 else "Not invoiced"


def _sales_orders(name: str) -> list[str]:
	return frappe.get_all(
		"Delivery Note Item",
		filters={"parent": name, "against_sales_order": ["is", "set"]},
		pluck="against_sales_order",
		distinct=True,
		order_by="against_sales_order asc",
	)


def _serialize_row(row) -> dict:
	return {
		"id": row.name,
		"date": row.posting_date,
		"dealer": row.customer,
		"dealerName": row.customer_name,
		"status": "Cancelled" if row.docstatus == 2 else "Submitted",
		"billing": _billing_label(row.per_billed),
		"transporter": row.get("transporter_name"),
		"vehicleNo": row.get("vehicle_no"),
		"lrNo": row.get("lr_no"),
		"totalQty": row.total_qty,
	}


@frappe.whitelist(methods=["GET"])
def list_delivery_notes(
	search: str | None = None,
	dealer: str | None = None,
	status: str | None = None,
	billing: str | None = None,
	sales_order: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	_assert_can_read()
	limit, offset = clamp(limit, offset)
	if status and status not in STATUSES:
		frappe.throw(_("Invalid status: {0}").format(status), frappe.ValidationError)
	if billing and billing not in BILLING:
		frappe.throw(_("Invalid billing: {0}").format(billing), frappe.ValidationError)

	# Never drafts: the app only ever posts a note, and a draft is someone mid-edit in the desk.
	conditions = ["docstatus in (1, 2)"]
	params: dict = {}
	if status:
		conditions = ["docstatus = %(docstatus)s"]
		params["docstatus"] = STATUSES[status]
	if dealer:
		conditions.append("customer = %(dealer)s")
		params["dealer"] = dealer
	if from_date:
		conditions.append("posting_date >= %(from_date)s")
		params["from_date"] = from_date
	if to_date:
		conditions.append("posting_date <= %(to_date)s")
		params["to_date"] = to_date
	if billing == "Not invoiced":
		conditions.append("ifnull(per_billed, 0) <= 0")
	elif billing == "Partly invoiced":
		conditions.append("per_billed > 0 and per_billed < 99.99")
	elif billing == "Fully invoiced":
		conditions.append("per_billed >= 99.99")
	if sales_order:
		conditions.append(
			"name in (select parent from `tabDelivery Note Item` where against_sales_order = %(sales_order)s)"
		)
		params["sales_order"] = sales_order.strip()
	if search:
		conditions.append(
			"("
			+ " or ".join(f"`{f}` like %(search)s" for f in _SEARCH_FIELDS)
			+ " or name in (select parent from `tabDelivery Note Item` where against_sales_order like %(search)s)"
			+ ")"
		)
		params["search"] = f"%{search.strip()}%"
	where = " and ".join(conditions)

	total = frappe.db.sql(f"select count(*) from `tabDelivery Note` where {where}", params)[0][0]
	rows = frappe.db.sql(
		f"""select name, posting_date, customer, customer_name, docstatus, per_billed, total_qty,
			transporter_name, vehicle_no, lr_no
		from `tabDelivery Note` where {where}
		order by posting_date desc, name desc limit %(limit)s offset %(offset)s""",
		{**params, "limit": limit, "offset": offset},
		as_dict=True,
	)
	return {"items": [_serialize_row(r) for r in rows], "total": total, "limit": limit, "offset": offset}


def _linked_invoices(name: str, orders: list[str]) -> list[str]:
	"""Posted invoices that bill this note or any of its sales orders. A migrated note has neither
	a sales order nor a billing link, and then has no invoices here -- the old query matched
	`sales_order in ('')`, which is every invoice line with no order, i.e. the whole ledger."""
	or_filters = [["delivery_note", "=", name]]
	if orders:
		or_filters.append(["sales_order", "in", orders])
	return frappe.get_all(
		"Sales Invoice Item",
		filters={"docstatus": 1},
		or_filters=or_filters,
		fields=["parent"],
		distinct=True,
		order_by="parent asc",
		pluck="parent",
	)


@frappe.whitelist(methods=["GET"])
def delivery_note_invoices(delivery_note: str, limit: int = 5, offset: int = 0):
	"""The next few invoices linked to a Delivery Note, for the detail panel's "Show more"."""
	_assert_can_read()
	limit, offset = clamp(limit, offset)
	invoices = _linked_invoices(delivery_note, _sales_orders(delivery_note))
	return {"items": invoices[offset : offset + limit], "total": len(invoices), "limit": limit, "offset": offset}


@frappe.whitelist(methods=["GET"])
def get_delivery_note(delivery_note: str):
	_assert_can_read()
	doc = frappe.get_doc("Delivery Note", delivery_note)
	if doc.docstatus == 0:
		frappe.throw(_("{0} is a draft, not a posted Delivery Note.").format(delivery_note), frappe.DoesNotExistError)

	orders = _sales_orders(doc.name)
	invoices = _linked_invoices(doc.name, orders)

	lines, line_weights = [], []
	for row in doc.items:
		per_box = (
			(frappe.db.get_value("Batch", row.batch_no, "custom_batch_weight_kg") or None) if row.get("batch_no") else None
		) or item_weight_per_box_kg(row.item_code)
		qty = row.get("stock_qty") or row.qty
		weight = round(per_box * qty, 2) if per_box and qty else None
		line_weights.append(weight)
		lines.append(
			{
				"itemCode": row.item_code,
				"itemName": row.item_name,
				"images": item_images(row.item_code),
				"qty": row.qty,
				"uom": row.uom,
				"warehouse": row.warehouse,
				"batchNo": row.get("batch_no"),
				"salesOrder": row.get("against_sales_order"),
				"weightKg": weight,
			}
		)

	return {
		**_serialize_row(doc),
		"salesOrders": orders,
		# Only the first five; the rest come from delivery_note_invoices ("Show more").
		"invoices": invoices[:5],
		"invoiceTotal": len(invoices),
		"address": doc.get("address_display"),
		"shippingAddress": doc.get("shipping_address"),
		"poNumber": doc.po_no,
		"driverName": doc.get("driver_name"),
		"remarks": doc.get("remarks"),
		# Only when every line's weight is known -- a partial sum would understate it.
		"totalWeightKg": round(sum(line_weights), 2) if line_weights and None not in line_weights else None,
		"items": lines,
	}
