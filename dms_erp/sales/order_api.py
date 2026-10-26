"""Orders — ERPNext's native Sales Order. Pacific's warehouse-fulfillment stages
(Confirmed -> Picking -> Ready to Dispatch -> Dispatched -> Delivered/Cancelled) are
a distinct operational flow layered on top via `custom_fulfillment_stage` and a
structured `custom_stage_history` (see sales/setup.py). Advancing to Dispatched posts
a real Delivery Note (see _create_delivery_note) — the one point in this app's whole
order lifecycle where Bin.actual_qty actually goes down for a sale; everywhere else
(reorder suggestions, the dealer portal's stock figures, dashboards) had been
overstating on-hand stock by everything ever sold until this existed.

An Order sourced directly from an Inquiry (no Quotation, no retail markup) is created
here; an Order sourced from a Quotation goes through quotation_api.convert_to_order,
which reuses ERPNext's own Quotation-to-Sales-Order mapper.

list_orders is paginated (`limit`/`offset`) and returns `{"items", "total",
"limit", "offset"}`, not a bare list.
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, now_datetime, today

from dms_erp.catalog.utils import (
	item_pieces_per_box,
	item_sqft_per_box,
	item_sqm_per_box,
	item_weight_per_box_kg,
	normalize_order_line_uoms,
)
from dms_erp.pagination import clamp
from dms_erp.pricing.api import get_price_for_dealer
from dms_erp.sales.order_channel import auto_classify_channel, gate_channel_override
from dms_erp.sales.reservation import reservation_summary
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
		"createdAt": doc.creation,
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
		"driverMobile": doc.get("custom_driver_mobile"),
		# BRD C.3.4 — interim manual gate ahead of VALS API. See advance_order_stage.
		"advanceConfirmed": bool(doc.custom_advance_confirmed),
		"owner": doc.owner,
		"history": [
			{"stage": row.stage, "at": row.at, "by": row.by, "note": row.note}
			for row in sorted(doc.custom_stage_history, key=lambda r: r.idx)
		],
		"invoices": _order_invoices(doc.name),
		"deliveryNotes": _order_delivery_notes(doc.name),
		# ISS-2026-00635 -- 21-day reservation timeline; see sales.reservation.
		"reservation": reservation_summary(doc),
	}


def _order_delivery_notes(order: str) -> list[dict]:
	"""Real data the moment a Dispatch has actually posted one (see advance_order_stage /
	_create_delivery_note); [] before then. Mirrors _order_invoices below -- same
	exclude-cancelled, one-frappe.get_doc-per-row shape."""
	names = frappe.db.sql(
		"select distinct parent from `tabDelivery Note Item` where against_sales_order=%s", (order,), as_dict=True
	)
	notes = []
	for row in names:
		docstatus = frappe.db.get_value("Delivery Note", row.parent, "docstatus")
		if docstatus != 2:
			doc = frappe.get_doc("Delivery Note", row.parent)
			notes.append(
				{
					"id": doc.name,
					"postingDate": doc.posting_date,
					"lines": [{"itemCode": row.item_code, "qty": row.qty, "warehouse": row.warehouse, "batchNo": row.batch_no} for row in doc.items],
				}
			)
	return notes


def _order_invoices(order: str) -> list[dict]:
	"""BRD Figure 4's Sales Invoice step (see create_sales_invoice) — real data
	the moment one exists for this order, empty list until then. Cancelled
	invoices are excluded; a cancelled-and-reinvoiced order should read as
	invoiced against the live one, not double-counted or hidden. Reuses
	_serialize_invoice (one frappe.get_doc per invoice -- normally 0-1 per
	order) rather than duplicating its field list, so weight (BRD C.1.3) and
	anything else added there stays consistent everywhere an invoice shows up,
	not just right after create_sales_invoice runs."""
	names = frappe.db.sql(
		"select distinct parent from `tabSales Invoice Item` where sales_order=%s", (order,), as_dict=True
	)
	invoices = []
	for row in names:
		docstatus = frappe.db.get_value("Sales Invoice", row.parent, "docstatus")
		if docstatus != 2:
			invoices.append(_serialize_invoice(frappe.get_doc("Sales Invoice", row.parent)))
	return invoices


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
		# Sheet 7.8 -- the originally entered qty/unit, only when it wasn't Box.
		"enteredQty": row.get("custom_entered_qty"),
		"enteredUom": row.get("custom_entered_uom"),
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
	duplicates = _possible_duplicate_orders(so)
	# The flag rides on the Confirmed entry's note rather than an extra history row, so
	# the stage history stays one row per stage.
	confirmed_note = (
		_("Possible duplicate of {0}: same dealer, items and quantities").format(", ".join(duplicates))
		if duplicates
		else None
	)
	so.append("custom_stage_history", {"stage": "Confirmed", "at": now, "by": frappe.session.user, "note": confirmed_note})

	so.insert(ignore_permissions=True)
	clear_unrequested_default_tax(so)
	if frappe.flags.get("dms_credit_limit_approved"):
		# BRD C.11: Management approved this order over the credit limit (see
		# sales.credit_limit.credit_limit_approved) -- don't let ERPNext's own check refuse it.
		so.check_credit_limit = lambda: None
	so.submit()
	out = _serialize(so)
	out["possibleDuplicates"] = duplicates
	return out


# Same window the duplicate-inquiry check uses (sales.utils.DUPLICATE_INQUIRY_WINDOW_DAYS).
DUPLICATE_ORDER_WINDOW_DAYS = 7


def _possible_duplicate_orders(so) -> list[str]:
	"""BRD C.2.4: "flags duplicate customer POs (same dealer, same quantity and material,
	different PO number) so the same order is not reserved or procured twice." Another
	live (submitted, not cancelled) order from the same dealer in the last
	DUPLICATE_ORDER_WINDOW_DAYS with exactly the same items and quantities, whatever its
	PO number (a different number is the BRD's case; the same one reused is just as
	suspect). A flag only -- noted on the order and returned to the
	caller, never a block: a dealer genuinely re-ordering the same thing is normal."""
	def signature(rows) -> tuple:
		qty: dict[str, float] = {}
		for r in rows:
			qty[r.item_code] = qty.get(r.item_code, 0) + flt(r.qty)
		return tuple(sorted(qty.items()))

	mine = signature(so.items)
	if not mine:
		return []
	since = add_days(today(), -DUPLICATE_ORDER_WINDOW_DAYS)
	candidates = frappe.get_all(
		"Sales Order",
		filters={
			"customer": so.customer,
			"docstatus": 1,
			"transaction_date": [">=", since],
			"custom_fulfillment_stage": ["!=", "Cancelled"],
		},
		pluck="name",
	)
	out = []
	for name in candidates:
		rows = frappe.get_all("Sales Order Item", filters={"parent": name}, fields=["item_code", "qty"])
		if signature([frappe._dict(r) for r in rows]) == mine:
			out.append(name)
	return out


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
		# ISS-2026-00630 / BRD §C.12: outward search is primarily by lorry number or
		# driver mobile, secondarily by Sales Invoice number. The invoice match goes
		# through Sales Invoice Item.sales_order -- the same link _order_invoices reads
		# -- since a Sales Order carries no invoice field of its own. Tally-migrated
		# invoices have no sales_order on their lines, so they never match here; they
		# stay findable from the Invoices screen (invoice_api.list_sales_invoices).
		pattern = ["like", f"%{search}%"]
		matches = (
			set(frappe.get_all("Sales Order", filters={"name": pattern}, pluck="name"))
			| set(frappe.get_all("Sales Order", filters={"custom_vehicle": pattern}, pluck="name"))
			| set(frappe.get_all("Sales Order", filters={"custom_driver_mobile": pattern}, pluck="name"))
			| set(
				frappe.get_all(
					"Sales Invoice Item",
					filters={"parent": pattern, "sales_order": ["is", "set"], "docstatus": ["!=", 2]},
					pluck="sales_order",
				)
			)
		)
		filters["name"] = ["in", list(matches) or [""]]
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
	from dms_erp.sales.quotation_api import _typed_rate

	typed_rate = _typed_rate(item, line, discount_pct)
	if typed_rate is not None:
		rate = typed_rate  # BRD C.11 #3, gated before creation (see create_order)
	return {
		"item_code": item,
		"qty": line["qty"],
		"price_list_rate": price_list_rate,
		"discount_percentage": discount_pct,
		"rate": rate,
		"delivery_date": line.get("delivery_date") or default_delivery_date,
		# Sheet 7.8 -- only set when the line was actually entered in a non-Box unit
		# (see catalog.utils.normalize_order_line_uoms); None/None for a plain box order.
		"custom_entered_qty": line.get("entered_qty"),
		"custom_entered_uom": line.get("entered_uom"),
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
	simply untaxed, same as any ERPNext site with no GST template configured.

	`channel` here is always already resolved (never None) -- create_order (the
	whitelisted wrapper) resolves and gates it before this ever runs; only
	dealer_portal_api.convert_to_order still calls this directly with channel
	unset, since a dealer's self-service order was never a BRD C.11#6 override
	candidate in the first place."""
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
	audit-locked manual override (BRD C.11 trigger #6) -- an unauthorized caller
	gets `{"approvalRequired": True, "approval": {...}}` back instead of a
	serialized order, and the Sales Order isn't created until Management approves
	the queued Approval Request (see sales.order_channel.gate_channel_override).

	Each line in `lines` may carry `discount_percentage` (0-100) and/or
	`delivery_date` -- see _priced_order_line. A line whose discount is above
	`DMS Sales Settings.discount_approval_threshold_pct` (0 by default -- see
	approvals.api.gate_discount_over_price_list) is BRD C.11 trigger #4 and
	gates the same way channel override does.
	So does a dealer whose committed order value (this order's own estimated
	value included) would exceed their configured credit limit (BRD C.11
	trigger #1 -- see sales.credit_limit.gate_credit_limit). `taxes_and_charges`
	names an existing Sales Taxes and Charges Template; see
	sales.utils.apply_tax_template.

	A line may also carry `uom` (sheet 7.8 -- Box/Pcs/Sqft/Sqm/Kg/Gram; Box when
	left unset), converted to boxes by normalize_order_line_uoms before anything
	else -- channel classification, the credit-limit estimate, pricing -- ever
	sees `qty`, since every one of those is a box-qty rule."""
	_assert_can_manage_orders()
	if not lines:
		frappe.throw(_("At least one line is required."), frappe.ValidationError)
	lines = normalize_order_line_uoms(lines)

	default_channel = auto_classify_channel(dealer, lines)
	resolved_channel = channel if channel is not None else default_channel

	create_kwargs = dict(
		dealer=dealer,
		lines=lines,
		expected_dispatch=expected_dispatch,
		inquiry=inquiry,
		channel=resolved_channel,
		customer_po=customer_po,
		taxes_and_charges=taxes_and_charges,
	)

	def _create_after_discount_gate(**kwargs):
		# BRD C.7.3/C.11#4: see quotation_api.create_quotation's own copy of this
		# nested gate for why it's nested inside the other gates rather than
		# checked separately.
		from dms_erp.approvals.api import gate_discount_over_price_list

		return gate_discount_over_price_list(
			reference_doctype="Sales Order",
			lines=kwargs["lines"],
			create_fn=_create_order,
			create_kwargs=kwargs,
		)

	def _create_after_credit_gate(**kwargs):
		from dms_erp.sales.credit_limit import gate_credit_limit

		return gate_credit_limit(
			dealer=kwargs["dealer"],
			additional_value=_estimate_order_value(kwargs["dealer"], kwargs["lines"]),
			reference_doctype="Sales Order",
			applier_action="create_order",
			create_fn=_create_after_discount_gate,
			create_kwargs=kwargs,
		)

	return gate_channel_override(
		requested_channel=resolved_channel,
		default_channel=default_channel,
		reference_doctype="Sales Order",
		create_fn=_create_after_credit_gate,
		create_kwargs=create_kwargs,
	)


def _estimate_order_value(dealer: str, lines: list[dict]) -> float:
	"""Pre-tax, pre-rounding approximate order value for the credit-limit gate
	(BRD C.11 trigger #1) -- the same kind of approximation dashboard.api.
	_credit_exposure_alerts already accepts for this app's whole credit-exposure
	signal (no Sales Invoice ledger exists to compute a true one from). Real
	per-line rate/rounding still happens in _priced_order_line at actual
	creation time; this is only ever used to decide whether to gate, never
	written anywhere."""
	total = 0.0
	for line in lines:
		price = get_price_for_dealer(line["item"], dealer) or 0
		discount_pct = float(line.get("discount_percentage") or 0)
		if line.get("rate") not in (None, ""):
			total += float(line["rate"]) * line["qty"]
			continue
		total += price * (1 - discount_pct / 100) * line["qty"]
	return total


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
def advance_order_stage(
	order: str, next_stage: str, note: str | None = None, vehicle: str | None = None, driver_mobile: str | None = None
):
	"""Moving to "Cancelled" is BRD C.11 trigger #5 (amend/cancel of a submitted
	document is audit-locked) -- an unauthorized caller gets
	`{"approvalRequired": True, "approval": {...}}` back instead of the updated
	order, and the order isn't actually cancelled until Management approves it
	(see approvals.api.gate_authorized_action / APPLIERS). Every other forward
	step is unaffected -- that's routine fulfillment progress, not a
	cancellation.

	`vehicle`/`driver_mobile` (ISS-2026-00630) are optional on every forward step,
	not just "Dispatched" -- the actual vehicle/driver is sometimes known earlier
	(e.g. at Ready to Dispatch) and whoever's progressing the order can record it
	as soon as it's known rather than being forced to wait for a specific stage."""
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

	if is_cancel:
		from dms_erp.approvals.api import gate_authorized_action

		return gate_authorized_action(
			trigger_type="Amend Or Cancel Submitted Document",
			reference_doctype="Sales Order",
			reference_name=order,
			reason=f"{frappe.session.user} is cancelling Sales Order {order} (was {current}).",
			action_fn=_cancel_order_action,
			action_kwargs=dict(order=order, note=note),
			applier_action="cancel",
		)

	if next_stage == "Dispatched":
		# Validated before anything is written: a blocked dispatch should leave the
		# order exactly as it was, not half-advanced with no stock movement to match.
		_assert_lines_ready_for_delivery(doc)

	doc.custom_fulfillment_stage = next_stage
	if vehicle:
		doc.custom_vehicle = vehicle
	if driver_mobile:
		doc.custom_driver_mobile = driver_mobile
	doc.append("custom_stage_history", {"stage": next_stage, "at": now_datetime(), "by": frappe.session.user, "note": note})
	doc.save(ignore_permissions=True)

	if next_stage == "Picking":
		from dms_erp.sales.picking_api import ensure_pick_tasks

		ensure_pick_tasks(order)
	elif next_stage == "Dispatched":
		_create_delivery_note(order)

	return _serialize(doc)


def _assert_lines_ready_for_delivery(doc) -> None:
	"""Every Sales Order line needs a Pick Task that's actually Picked -- staff's own
	"Mark picked" confirmation (picking.tsx) -- before dispatch can post a real stock
	movement against it. `suggested_bay` is only ever ensure_pick_tasks's initial
	category-based guess, never confirmed by the picking screen's own flow (it doesn't
	ask staff to correct it before "Mark picked"), so it's a signal that picking is
	underway, not where the stock dispatch will actually draw from -- see
	_resolve_delivery_bay_and_batch, which finds that from real on-hand stock instead.
	A line with no Pick Task, or one not yet Picked, blocks the move rather than
	silently skipping the stock effect of a real dispatch."""
	picked = set(
		frappe.get_all("Pick Task", filters={"sales_order": doc.name, "status": "Picked"}, pluck="sales_order_item")
	)
	missing = [row.item_code for row in doc.items if row.name not in picked]
	if missing:
		frappe.throw(
			_("{0} has {1} not yet marked picked -- confirm picking before dispatch.").format(doc.name, ", ".join(missing)),
			frappe.ValidationError,
		)


def _resolve_delivery_bay_and_batch(item: str, qty: float) -> tuple[str, str]:
	"""Which bay and batch a Delivery Note line should actually draw from, for `item`.
	Reads the same ground truth the Stock View page already uses
	(warehouse.utils.list_stock_lots) rather than Pick Task's own `suggested_bay`,
	which is only ever an unconfirmed initial guess (see
	_assert_lines_ready_for_delivery). Only ever auto-resolves the unambiguous case --
	exactly one bay+batch with enough on hand across the whole warehouse; none, or
	more than one, blocks the dispatch with a clear reason rather than guessing which
	one staff meant or silently splitting the line across bays/batches Pick Task never
	recorded a split for."""
	from dms_erp.warehouse.utils import list_stock_lots

	lots = [l for l in list_stock_lots(item=item) if l["boxes"] >= qty]
	if len(lots) == 1:
		return lots[0]["bayId"], lots[0]["batchNumber"]
	if not lots:
		frappe.throw(
			_("No single bay has {0} boxes of {1} in one batch to dispatch -- split the pick across bays in Picking first.").format(
				qty, item
			),
			frappe.ValidationError,
		)
	frappe.throw(
		_("{0} is split across {1} bays/batches with {2}+ boxes each -- pick from a single one before dispatch.").format(
			item, len(lots), qty
		),
		frappe.ValidationError,
	)


def _create_delivery_note(order: str) -> dict:
	"""BRD C.3.1 / Figure 4 -- "if stock is available it converts to delivery [...]":
	the real stock-decrement event for this order, which nothing in this app's order
	lifecycle had ever actually posted (Bin.actual_qty only ever went up, via
	allocation_api's Purchase Receipt at inward, never down for a sale going out).
	Posted automatically the instant an order reaches Dispatched (see
	advance_order_stage) -- the same "the real stock-effecting event happens on its
	own, not a separate manual step staff can forget" pattern allocation_api already
	uses for receipt, rather than a second button for staff to remember to click.

	Uses ERPNext's own Sales Order -> Delivery Note mapper (ERPNext's actual
	stock-ledger-posting document) so items/rates are carried over from the
	already-approved order, never re-derived; each line's warehouse and batch come
	from _resolve_delivery_bay_and_batch (every line must already be Picked -- see
	_assert_lines_ready_for_delivery, called first)."""
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	doc = frappe.get_doc("Sales Order", order)
	_assert_lines_ready_for_delivery(doc)

	dn = make_delivery_note(order)
	for row in dn.items:
		row.warehouse, row.batch_no = _resolve_delivery_bay_and_batch(row.item_code, row.qty)
	dn.insert(ignore_permissions=True)
	dn.submit()
	return {"id": dn.name}


def _cancel_order_action(order: str, note: str | None = None) -> dict:
	"""Unguarded core of the Cancelled transition -- see gate_authorized_action.
	Re-checks the order's current stage at call time (not just at gate time),
	since nothing stops it moving on between an unauthorized request being
	queued and Management deciding it."""
	doc = frappe.get_doc("Sales Order", order)
	current = doc.custom_fulfillment_stage
	if current in ("Delivered", "Cancelled"):
		frappe.throw(_("Cannot move an order from {0} to Cancelled.").format(current), frappe.ValidationError)

	doc.custom_fulfillment_stage = "Cancelled"
	doc.append("custom_stage_history", {"stage": "Cancelled", "at": now_datetime(), "by": frappe.session.user, "note": note})
	doc.save(ignore_permissions=True)
	return _serialize(doc)


def _serialize_invoice(doc) -> dict:
	"""BRD C.1.3 -- "Weight (in the correct UOM) must be displayed on every
	relevant document... invoice..." -- per-line and invoice-total weight,
	same item_weight_per_box_kg derivation every other document here already
	uses (Inquiry/Quotation/Sales Order/Purchase Order/Bay Allocation/
	stickers/Pickup Run all had this; this was the one left out when Sales
	Invoice itself was first built)."""
	lines = []
	total_weight_kg = 0.0
	any_weight = False
	for row in doc.items:
		weight_per_box_kg = item_weight_per_box_kg(row.item_code)
		line_weight_kg = (weight_per_box_kg or 0) * row.qty if weight_per_box_kg is not None else None
		if line_weight_kg is not None:
			any_weight = True
			total_weight_kg += line_weight_kg
		lines.append(
			{
				"itemCode": row.item_code,
				"qty": row.qty,
				"rate": row.rate,
				"amount": row.amount,
				"weightPerBoxKg": weight_per_box_kg,
				"totalWeightKg": line_weight_kg,
			}
		)
	return {
		"id": doc.name,
		"date": doc.posting_date,
		"dealerId": doc.customer,
		"salesOrder": doc.items[0].sales_order if doc.items else None,
		"grandTotal": doc.grand_total,
		"outstandingAmount": doc.outstanding_amount,
		"status": doc.status,
		"channel": doc.get("custom_order_channel"),
		"lines": lines,
		"totalWeightKg": round(total_weight_kg, 2) if any_weight else None,
	}


@frappe.whitelist(methods=["POST"])
def create_sales_invoice(order: str):
	"""BRD Figure 4's order-to-cash flow (Sales Order -> reserve stock -> Delivery
	Note -> Sales Invoice + e-Way bill -> Payment Tracking) has never had a Sales
	Invoice step anywhere in this app -- every "this app posts no Sales Invoice"
	caveat across sales.credit_limit, dashboard.api, dealer_portal_api.my_dues
	and approvals.api's trigger #2 note is about this exact gap. This uses
	ERPNext's own Sales Order -> Sales Invoice mapper (make_sales_invoice) so
	items/rates/taxes are carried over from the already-approved order, never
	re-derived, then inserts and submits a real Sales Invoice the same way
	every other action here creates its document (ignore_permissions=True,
	gated by role instead).

	Gated to Dispatched/Delivered -- billing an order that hasn't actually
	started moving has no basis in the BRD's own diagram or in ordinary
	invoicing practice. One live invoice per order: a second call against an
	already-invoiced order is rejected rather than silently double-billing it.

	The Delivery Note step the BRD diagram shows ahead of this now posts
	automatically at Dispatched (see _create_delivery_note) -- this function only
	ever handles the Sales Invoice. Real payment/AR tracking after it remains its
	own separate, larger, not-yet-scoped piece of work. _order_invoices (see
	_serialize) starts returning real rows for this order the moment this runs."""
	_assert_can_manage_orders()

	doc = frappe.get_doc("Sales Order", order)
	if doc.custom_fulfillment_stage not in ("Dispatched", "Delivered"):
		frappe.throw(
			_("{0} must be Dispatched or Delivered before it can be invoiced (currently {1}).").format(
				order, doc.custom_fulfillment_stage
			),
			frappe.ValidationError,
		)
	if _order_invoices(order):
		frappe.throw(_("{0} is already invoiced.").format(order), frappe.ValidationError)

	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	invoice = make_sales_invoice(order, ignore_permissions=True)
	# BRD C.4.3 -- the retail/bulk tag follows the order onto its invoice.
	invoice.custom_order_channel = doc.custom_order_channel
	invoice.insert(ignore_permissions=True)
	invoice.submit()

	return _serialize_invoice(invoice)
