"""Orders — ERPNext's native Sales Order. Pacific's warehouse-fulfillment stages
(Confirmed -> Picking -> Ready to Dispatch -> Dispatched -> Delivered/Cancelled) are
a distinct operational flow layered on top via `custom_fulfillment_stage` and a
structured `custom_stage_history` (see sales/setup.py) — advancing them here does
not itself create a Delivery Note or move stock; that's a natural future refinement
once a real dispatch/delivery step is in scope.

An Order sourced directly from an Inquiry (no Quotation, no retail markup) is created
here; an Order sourced from a Quotation goes through quotation_api.convert_to_order,
which reuses ERPNext's own Quotation-to-Sales-Order mapper.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from dms_erp.pricing.api import get_dealer_price
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
		"lines": [{"itemCode": row.item_code, "qty": row.qty, "rate": row.rate} for row in doc.items],
		# Server-computed only — every line's rate came from get_dealer_price at
		# creation time, never a client-supplied value, so this total is trustworthy.
		"total": doc.grand_total,
		"stage": doc.custom_fulfillment_stage,
		"expectedDispatch": doc.delivery_date,
		"vehicle": doc.custom_vehicle,
		"owner": doc.owner,
		"history": [
			{"stage": row.stage, "at": row.at, "by": row.by, "note": row.note}
			for row in sorted(doc.custom_stage_history, key=lambda r: r.idx)
		],
	}


def finalize_new_order(so, source_type: str, source_ref: str, channel: str = "Retail") -> dict:
	"""Insert + submit a freshly-built (unsaved) Sales Order doc, stamping Pacific's
	fulfillment-stage bookkeeping. Shared by create_order and quotation_api.convert_to_order."""
	if channel not in ORDER_CHANNELS:
		frappe.throw(_("Invalid channel: {0}").format(channel), frappe.ValidationError)

	so.custom_source_type = source_type
	so.custom_source_ref = source_ref
	so.custom_order_channel = channel
	so.custom_fulfillment_stage = "Confirmed"

	now = now_datetime()
	so.append("custom_stage_history", {"stage": "Created", "at": now, "by": frappe.session.user, "note": f"Converted from {source_ref}"})
	so.append("custom_stage_history", {"stage": "Confirmed", "at": now, "by": frappe.session.user})

	so.insert(ignore_permissions=True)
	so.submit()
	return _serialize(so)


@frappe.whitelist(methods=["GET"])
def list_orders(dealer: str | None = None, stage: str | None = None):
	filters = {}
	if dealer:
		filters["customer"] = dealer
	if stage:
		filters["custom_fulfillment_stage"] = stage
	names = frappe.get_all("Sales Order", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Sales Order", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def get_order(order: str):
	return _serialize(frappe.get_doc("Sales Order", order))


@frappe.whitelist(methods=["POST"])
def create_order(dealer: str, lines: list[dict], expected_dispatch, inquiry: str, channel: str = "Retail"):
	"""Direct Inquiry -> Order conversion (no Quotation, no retail markup — matches
	how o1/o4/o6 in the frontend's seed data go straight from Inquiry to Order at
	plain approved dealer-price rates). The Quotation-sourced path is
	quotation_api.convert_to_order; there is no third, source-less way to create an
	Order, mirroring the frontend's Order.sourceType being strictly "Inquiry" or
	"Quotation"."""
	_assert_can_manage_orders()

	if not lines:
		frappe.throw(_("At least one line is required."), frappe.ValidationError)

	items = []
	for line in lines:
		item = line["item"]
		rate = get_dealer_price(item)
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
			"items": items,
		}
	)

	order = finalize_new_order(so, source_type="Inquiry", source_ref=inquiry, channel=channel)
	frappe.db.set_value("Inquiry", inquiry, "status", "Converted to Order")

	return order


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
