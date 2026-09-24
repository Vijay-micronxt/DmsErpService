"""Orders — ERPNext's native Sales Order. Pacific's warehouse-fulfillment stages
(Confirmed -> Picking -> Ready to Dispatch -> Dispatched -> Delivered/Cancelled) are
a distinct operational flow layered on top via `custom_fulfillment_stage` and a
structured `custom_stage_history` (see sales/setup.py) — advancing them here does
not itself create a Delivery Note or move stock; that's a natural future refinement
once a real dispatch/delivery step is in scope.

An Order sourced directly from an Inquiry (no Quotation, no retail markup) is created
here; an Order sourced from a Quotation goes through quotation_api.convert_to_order,
which reuses ERPNext's own Quotation-to-Sales-Order mapper.

list_orders is paginated (`limit`/`offset`) and returns `{"items", "total",
"limit", "offset"}`, not a bare list.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from dms_erp.catalog.utils import item_weight_per_box_kg
from dms_erp.pagination import clamp
from dms_erp.pricing.api import get_price_for_dealer
from dms_erp.sales.order_channel import auto_classify_channel
from dms_erp.sales.setup import ORDER_CHANNELS, ORDER_STAGES
from dms_erp.warehouse.utils import default_company

ORDER_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
FORWARD_FLOW = ["Confirmed", "Picking", "Ready to Dispatch", "Dispatched", "Delivered"]


def _assert_can_manage_orders():
	if not set(frappe.get_roles(frappe.session.user)) & ORDER_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage orders."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"number": doc.name,
		"date": doc.transaction_date,
		"dealerId": doc.customer,
		"sourceType": doc.custom_source_type,
		"sourceRef": doc.custom_source_ref,
		"channel": doc.custom_order_channel,
		"lines": [_serialize_line(row) for row in doc.items],
		# Server-computed only — every line's rate came from get_price_for_dealer at
		# creation time, never a client-supplied value, so this total is trustworthy.
		"total": doc.grand_total,
		"stage": doc.custom_fulfillment_stage,
		"customerPo": doc.po_no,
		"expectedDispatch": doc.delivery_date,
		"vehicle": doc.custom_vehicle,
		"owner": doc.owner,
		"history": [
			{"stage": row.stage, "at": row.at, "by": row.by, "note": row.note}
			for row in sorted(doc.custom_stage_history, key=lambda r: r.idx)
		],
	}


def _serialize_line(row) -> dict:
	weight_per_box_kg = item_weight_per_box_kg(row.item_code)
	return {
		"itemCode": row.item_code,
		"qty": row.qty,
		"rate": row.rate,
		"weightPerBoxKg": weight_per_box_kg,
		"totalWeightKg": (weight_per_box_kg or 0) * row.qty if weight_per_box_kg is not None else None,
	}


def finalize_new_order(so, source_type: str, source_ref: str | None, channel: str = "Retail") -> dict:
	"""Insert + submit a freshly-built (unsaved) Sales Order doc, stamping Pacific's
	fulfillment-stage bookkeeping. Shared by create_order and quotation_api.convert_to_order."""
	if channel not in ORDER_CHANNELS:
		frappe.throw(_("Invalid channel: {0}").format(channel), frappe.ValidationError)

	so.custom_source_type = source_type
	so.custom_source_ref = source_ref
	so.custom_order_channel = channel
	so.custom_fulfillment_stage = "Confirmed"

	now = now_datetime()
	created_note = f"Converted from {source_ref}" if source_ref else "Created directly, no source Inquiry/Quotation"
	so.append("custom_stage_history", {"stage": "Created", "at": now, "by": frappe.session.user, "note": created_note})
	so.append("custom_stage_history", {"stage": "Confirmed", "at": now, "by": frappe.session.user})

	so.insert(ignore_permissions=True)
	so.submit()
	return _serialize(so)


@frappe.whitelist(methods=["GET"])
def list_orders(
	dealer: str | None = None, stage: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0
):
	limit, offset = clamp(limit, offset)
	filters = {}
	if dealer:
		filters["customer"] = dealer
	if stage:
		filters["custom_fulfillment_stage"] = stage
	if search:
		filters["name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Sales Order", filters=filters)
	names = frappe.get_all(
		"Sales Order", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Sales Order", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_order(order: str):
	return _serialize(frappe.get_doc("Sales Order", order))


def _create_order(
	dealer: str,
	lines: list[dict],
	expected_dispatch,
	inquiry: str | None = None,
	channel: str | None = None,
	customer_po: str | None = None,
) -> dict:
	"""Unguarded core of create_order -- also called directly by
	sales.dealer_portal_api.convert_to_order, whose own DMS Dealer session (scoped
	to its own dealer identity) is the authorization for that path, not
	ORDER_WRITE_ROLES. `customer_po` (BRD C.13.1 — the dealer's own PO number) sets
	the native Sales Order.po_no and closes the source Inquiry (BRD C.3.1) when
	there is one. `inquiry` is optional -- a staff-raised order with no prior
	Inquiry/Quotation behind it (a walk-in or phone sale) is source_type "Direct",
	same rate/catalog rules as any other order, just nothing to close on creation."""
	if not lines:
		frappe.throw(_("At least one line is required."), frappe.ValidationError)
	if channel is None:
		channel = auto_classify_channel(dealer, lines)

	items = []
	for line in lines:
		item = line["item"]
		rate = get_price_for_dealer(item, dealer)
		if rate is None:
			frappe.throw(_("{0} has no approved dealer price yet.").format(item), frappe.ValidationError)
		items.append({"item_code": item, "qty": line["qty"], "rate": rate, "delivery_date": expected_dispatch})

	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"customer": dealer,
			"company": default_company(),
			"transaction_date": today(),
			"delivery_date": expected_dispatch,
			"po_no": customer_po,
			"items": items,
		}
	)

	order = finalize_new_order(
		so, source_type="Inquiry" if inquiry else "Direct", source_ref=inquiry, channel=channel
	)
	if inquiry:
		values = {"status": "Converted to Order", "linked_sales_order": order["id"]}
		if customer_po:
			values["customer_po"] = customer_po
		frappe.db.set_value("Inquiry", inquiry, values)

	return order


@frappe.whitelist(methods=["POST"])
def create_order(dealer: str, lines: list[dict], expected_dispatch, inquiry: str | None = None, channel: str | None = None, customer_po: str | None = None):
	"""Direct Inquiry -> Order conversion (no Quotation, no retail markup — matches
	how o1/o4/o6 in the frontend's seed data go straight from Inquiry to Order at
	plain approved dealer-price rates). The Quotation-sourced path is
	quotation_api.convert_to_order.

	`inquiry` left unset creates a standalone order with no Inquiry/Quotation
	behind it (source_type "Direct") -- a walk-in or phone sale a dealer never
	raised a formal enquiry for. `customer_po` is the caller's PO number
	reference, same field an inquiry-sourced order sets via its own PO.

	`channel` left unset auto-classifies from the dealer's type / item Series
	thresholds (BRD C.4.3); passing one explicitly (including "Retail") is the
	audit-locked manual override."""
	_assert_can_manage_orders()
	return _create_order(dealer, lines, expected_dispatch, inquiry, channel, customer_po)


@frappe.whitelist(methods=["POST", "PUT"])
def advance_order_stage(order: str, next_stage: str, note: str | None = None):
	_assert_can_manage_orders()

	if next_stage not in ORDER_STAGES:
		frappe.throw(_("Invalid stage: {0}").format(next_stage), frappe.ValidationError)

	doc = frappe.get_doc("Sales Order", order)
	current = doc.custom_fulfillment_stage

	is_forward_step = current in FORWARD_FLOW and next_stage in FORWARD_FLOW and FORWARD_FLOW.index(next_stage) == FORWARD_FLOW.index(current) + 1
	is_cancel = next_stage == "Cancelled" and current != "Delivered" and current != "Cancelled"
	if not (is_forward_step or is_cancel):
		frappe.throw(_("Cannot move an order from {0} to {1}.").format(current, next_stage), frappe.ValidationError)

	doc.custom_fulfillment_stage = next_stage
	doc.append("custom_stage_history", {"stage": next_stage, "at": now_datetime(), "by": frappe.session.user, "note": note})
	doc.save(ignore_permissions=True)

	if next_stage == "Picking":
		from dms_erp.sales.picking_api import ensure_pick_tasks

		ensure_pick_tasks(order)

	return _serialize(doc)
