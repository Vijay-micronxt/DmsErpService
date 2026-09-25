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

from dms_erp.catalog.utils import (
	item_pieces_per_box,
	item_sqft_per_box,
	item_sqm_per_box,
	item_weight_per_box_kg,
)
from dms_erp.pagination import clamp
from dms_erp.pricing.api import get_price_for_dealer
from dms_erp.sales.order_channel import auto_classify_channel
from dms_erp.sales.setup import ORDER_CHANNELS, ORDER_STAGES
from dms_erp.sales.utils import apply_tax_template, clear_unrequested_default_tax
from dms_erp.warehouse.utils import default_company

ORDER_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
# No DMS Finance role exists anywhere in this app's role model -- Management is the
# closest fit for a payment sign-off gate, deliberately narrower than
# ORDER_WRITE_ROLES (a salesperson shouldn't be able to self-certify their own
# order's advance payment).
ADVANCE_CONFIRM_ROLES = {"DMS Management", "System Manager"}
FORWARD_FLOW = ["Confirmed", "Picking", "Ready to Dispatch", "Dispatched", "Delivered"]


def _assert_can_manage_orders():
	if not set(frappe.get_roles(frappe.session.user)) & ORDER_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage orders."), frappe.PermissionError)


def _assert_can_confirm_advance():
	if not set(frappe.get_roles(frappe.session.user)) & ADVANCE_CONFIRM_ROLES:
		frappe.throw(_("Only Management can confirm advance payment."), frappe.PermissionError)


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
		"netTotal": doc.net_total,
		# Which Sales Taxes and Charges Template (if any) priced the tax rows below —
		# never computed here, only ever copied from that template (see sales.utils.
		# apply_tax_template) and totalled by ERPNext's own calculate_taxes_and_totals.
		"taxesAndCharges": doc.taxes_and_charges,
		"taxes": [
			{"accountHead": row.account_head, "description": row.description, "rate": row.rate, "amount": row.tax_amount}
			for row in doc.taxes
		],
		"totalTaxesAndCharges": doc.total_taxes_and_charges,
		"stage": doc.custom_fulfillment_stage,
		"customerPo": doc.po_no,
		"expectedDispatch": doc.delivery_date,
		"vehicle": doc.custom_vehicle,
		# BRD C.3.4 — interim manual gate ahead of VALS API. See advance_order_stage.
		"advanceConfirmed": bool(doc.custom_advance_confirmed),
		"owner": doc.owner,
		"history": [
			{"stage": row.stage, "at": row.at, "by": row.by, "note": row.note}
			for row in sorted(doc.custom_stage_history, key=lambda r: r.idx)
		],
	}


def _serialize_line(row) -> dict:
	weight_per_box_kg = item_weight_per_box_kg(row.item_code)
	pieces_per_box = item_pieces_per_box(row.item_code)
	sqft_per_box = item_sqft_per_box(row.item_code)
	sqm_per_box = item_sqm_per_box(row.item_code)
	return {
		"itemCode": row.item_code,
		"qty": row.qty,
		# priceListRate is the undiscounted dealer-tier rate get_price_for_dealer
		# resolved; rate is what's actually charged after discountPercentage.
		# Both are always server-derived — discountPercentage is the only
		# caller-supplied number in this line, and it only ever scales the
		# already-approved rate down, never replaces it.
		"priceListRate": row.price_list_rate,
		"discountPercentage": row.discount_percentage,
		"rate": row.rate,
		"amount": row.amount,
		"deliveryDate": row.delivery_date,
		"weightPerBoxKg": weight_per_box_kg,
		"totalWeightKg": (weight_per_box_kg or 0) * row.qty if weight_per_box_kg is not None else None,
		"piecesPerBox": pieces_per_box,
		"totalPieces": (pieces_per_box or 0) * row.qty if pieces_per_box is not None else None,
		"sqftPerBox": sqft_per_box,
		"totalSqft": (sqft_per_box or 0) * row.qty if sqft_per_box is not None else None,
		"sqmPerBox": sqm_per_box,
		"totalSqm": round((sqm_per_box or 0) * row.qty, 4) if sqm_per_box is not None else None,
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
	clear_unrequested_default_tax(so)
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


def _priced_order_line(item: str, dealer: str, line: dict, default_delivery_date) -> dict:
	"""discount_percentage (0-100, optional) only ever scales down the server-
	resolved dealer-tier rate -- it's never a substitute for it. price_list_rate
	keeps the undiscounted rate on the row (native Sales Order Item field, same
	as ERPNext's own discount UI) so the discount is always auditable against
	what the dealer's tier actually approved. delivery_date (optional) overrides
	the order-level expected_dispatch for just this line -- a part shipment on a
	different date than the rest of the order."""
	price_list_rate = get_price_for_dealer(item, dealer)
	if price_list_rate is None:
		frappe.throw(_("{0} has no approved dealer price yet.").format(item), frappe.ValidationError)
	discount_pct = float(line.get("discount_percentage") or 0)
	if not 0 <= discount_pct <= 100:
		frappe.throw(_("Discount for {0} must be between 0 and 100%.").format(item), frappe.ValidationError)
	# No discount -> pass the approved rate through byte-for-byte, same as before
	# discount existed here; only an actual discount introduces new rounding.
	rate = round(price_list_rate * (1 - discount_pct / 100), 2) if discount_pct else price_list_rate
	return {
		"item_code": item,
		"qty": line["qty"],
		"price_list_rate": price_list_rate,
		"discount_percentage": discount_pct,
		"rate": rate,
		"delivery_date": line.get("delivery_date") or default_delivery_date,
	}


def _create_order(
	dealer: str,
	lines: list[dict],
	expected_dispatch,
	inquiry: str | None = None,
	channel: str | None = None,
	customer_po: str | None = None,
	taxes_and_charges: str | None = None,
) -> dict:
	"""Unguarded core of create_order -- also called directly by
	sales.dealer_portal_api.convert_to_order, whose own DMS Dealer session (scoped
	to its own dealer identity) is the authorization for that path, not
	ORDER_WRITE_ROLES. `customer_po` (BRD C.13.1 — the dealer's own PO number) sets
	the native Sales Order.po_no and closes the source Inquiry (BRD C.3.1) when
	there is one. `inquiry` is optional -- a staff-raised order with no prior
	Inquiry/Quotation behind it (a walk-in or phone sale) is source_type "Direct",
	same rate/catalog rules as any other order, just nothing to close on creation.
	`taxes_and_charges` (optional) names an existing Sales Taxes and Charges
	Template -- see sales.utils.apply_tax_template; left unset, the order is
	simply untaxed, same as any ERPNext site with no GST template configured."""
	if not lines:
		frappe.throw(_("At least one line is required."), frappe.ValidationError)
	if channel is None:
		channel = auto_classify_channel(dealer, lines)

	items = [_priced_order_line(line["item"], dealer, line, expected_dispatch) for line in lines]

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
	apply_tax_template(so, taxes_and_charges)

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
def create_order(
	dealer: str,
	lines: list[dict],
	expected_dispatch,
	inquiry: str | None = None,
	channel: str | None = None,
	customer_po: str | None = None,
	taxes_and_charges: str | None = None,
):
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
	audit-locked manual override.

	Each line in `lines` may carry `discount_percentage` (0-100) and/or
	`delivery_date` -- see _priced_order_line. `taxes_and_charges` names an
	existing Sales Taxes and Charges Template; see sales.utils.apply_tax_template."""
	_assert_can_manage_orders()
	return _create_order(dealer, lines, expected_dispatch, inquiry, channel, customer_po, taxes_and_charges)


@frappe.whitelist(methods=["POST", "PUT"])
def confirm_advance_payment(order: str, confirmed: bool = True):
	"""BRD C.3.4 — interim manual gate ahead of the real VALS API integration
	(blocked on external credentials, not built here). No dealer-level "requires
	advance" concept exists in this app -- Management confirms (or reverses)
	per order, whenever, same as any other manual sign-off; advance_order_stage
	is what actually enforces it against Ready to Dispatch."""
	_assert_can_confirm_advance()

	frappe.db.set_value("Sales Order", order, "custom_advance_confirmed", 1 if confirmed else 0)
	return get_order(order)


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

	if next_stage == "Ready to Dispatch" and not doc.custom_advance_confirmed:
		frappe.throw(
			_("{0}'s advance payment must be confirmed before it can move to Ready to Dispatch.").format(order),
			frappe.ValidationError,
		)

	doc.custom_fulfillment_stage = next_stage
	doc.append("custom_stage_history", {"stage": next_stage, "at": now_datetime(), "by": frappe.session.user, "note": note})
	doc.save(ignore_permissions=True)

	if next_stage == "Picking":
		from dms_erp.sales.picking_api import ensure_pick_tasks

		ensure_pick_tasks(order)

	return _serialize(doc)
