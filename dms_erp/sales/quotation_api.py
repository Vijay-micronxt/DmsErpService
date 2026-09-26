"""Quotation Builder (BRD §7.6 retail markup) — ERPNext's native Quotation doctype,
submitted immediately on creation (same one-action pattern as Phase 4's Purchase
Order — the frontend's builder has no separate draft step either). Rates are always
computed server-side from the approved dealer price (never client-supplied), and
every line must be in the dealer's assigned catalog and currently sellable — all
BRD-mandated gates, not just report-time checks. Sellability is checked here
directly (not just via Phase 11's `catalog_for` filter) because `is_visible` is a
pure per-dealer assignment flag — an item pulled back after being assigned visible
stays "visible" until Purchase removes it, so a line-level check is the real gate.

Line editing (Phase 12) can't just mutate `doc.items` in place — a submitted native
Quotation is immutable outside `allow_on_submit` fields, and `items` isn't one.
`add_quotation_line`/`remove_quotation_line`/`update_quotation_line_qty` all go
through ERPNext's standard amend cycle instead: cancel the current submission,
`frappe.copy_doc` it forward with `amended_from` set (giving the new document
Frappe's normal "-1" amended name), rebuild every line's rate from the *current*
approved dealer price and the quotation's own stored markup — never just editing the
one line asked for and leaving the rest stale — then insert and resubmit. The
quotation's `id` changes on every edit, same as amending any other submitted
ERPNext document; callers should always use the `id` a write endpoint returns, not
the one they started with.

list_quotations is paginated (`limit`/`offset`) and returns `{"items", "total",
"limit", "offset"}`, not a bare list.
"""

import frappe
from frappe import _
from frappe.utils import add_days, today

from dms_erp.catalog.dealer_catalog_api import is_visible
from dms_erp.catalog.utils import (
	is_sellable,
	item_pieces_per_box,
	item_sqft_per_box,
	item_sqm_per_box,
	item_weight_per_box_kg,
)
from dms_erp.pagination import clamp
from dms_erp.pricing.api import get_price_for_dealer
from dms_erp.sales.order_channel import auto_classify_channel
from dms_erp.sales.setup import ORDER_CHANNELS
from dms_erp.sales.utils import apply_tax_template, clear_unrequested_default_tax
from dms_erp.warehouse.utils import default_company

QUOTATION_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
NON_EDITABLE_STATUSES = {"Ordered", "Lost", "Cancelled", "Expired"}


def _assert_can_manage_quotations():
	if not set(frappe.get_roles(frappe.session.user)) & QUOTATION_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage quotations."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"number": doc.name,
		"date": doc.transaction_date,
		"createdAt": doc.creation,
		"dealerId": doc.party_name,
		"validTill": doc.valid_till,
		"markupPct": doc.custom_markup_pct,
		"freight": doc.custom_freight,
		"inquiryId": doc.custom_inquiry,
		"channel": doc.custom_order_channel,
		"lines": [_serialize_line(row) for row in doc.items],
		"total": doc.grand_total,
		"netTotal": doc.net_total,
		# See order_api._serialize's matching fields -- same "never computed here,
		# only ever copied from the template and totalled by ERPNext" contract.
		"taxesAndCharges": doc.taxes_and_charges,
		"taxes": [
			{"accountHead": row.account_head, "description": row.description, "rate": row.rate, "amount": row.tax_amount}
			for row in doc.taxes
		],
		"totalTaxesAndCharges": doc.total_taxes_and_charges,
		"status": doc.status,
	}


def _serialize_line(row) -> dict:
	weight_per_box_kg = item_weight_per_box_kg(row.item_code)
	pieces_per_box = item_pieces_per_box(row.item_code)
	sqft_per_box = item_sqft_per_box(row.item_code)
	sqm_per_box = item_sqm_per_box(row.item_code)
	return {
		"itemCode": row.item_code,
		"qty": row.qty,
		# priceListRate is the pre-discount marked-up rate (dealer_price * (1 +
		# markup_pct/100)); rate is what's actually quoted after discountPercentage.
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


def _priced_items(dealer: str, markup_pct: float, lines: list[dict]) -> list[dict]:
	items = []
	for line in lines:
		item = line["item"]
		if not is_visible(dealer, item):
			frappe.throw(_("{0} is not in this dealer's assigned catalog.").format(item), frappe.PermissionError)
		status = frappe.get_cached_value("Item", item, "custom_discontinuation_status") or "Active"
		if not is_sellable(status):
			frappe.throw(_("{0} is {1} and can no longer be quoted.").format(item, status), frappe.ValidationError)
		dealer_price = get_price_for_dealer(item, dealer)
		if dealer_price is None:
			frappe.throw(_("{0} has no approved dealer price yet.").format(item), frappe.ValidationError)
		price_list_rate = round(dealer_price * (1 + float(markup_pct) / 100))
		discount_pct = float(line.get("discount_percentage") or 0)
		if not 0 <= discount_pct <= 100:
			frappe.throw(_("Discount for {0} must be between 0 and 100%.").format(item), frappe.ValidationError)
		rate = round(price_list_rate * (1 - discount_pct / 100))
		items.append(
			{
				"item_code": item,
				"qty": line["qty"],
				"price_list_rate": price_list_rate,
				"discount_percentage": discount_pct,
				"rate": rate,
				"delivery_date": line.get("delivery_date"),
			}
		)
	return items


def _lines_from_items(rows) -> list[dict]:
	"""Reconstructs an editable `lines` list from a Quotation's current items --
	used by add/remove/update_quotation_line_qty before calling _amend_with_lines,
	which reprices from scratch. Rate/price_list_rate are deliberately NOT carried
	over (that's the whole point of re-pricing on every edit), but
	discount_percentage and delivery_date are a viewer's own choice, not something
	editing an unrelated line should silently reset to 0/unset."""
	return [
		{
			"item": row.item_code,
			"qty": row.qty,
			"discount_percentage": row.discount_percentage,
			"delivery_date": row.delivery_date,
		}
		for row in rows
	]


def _guard_editable(doc):
	if doc.docstatus != 1:
		frappe.throw(_("Only a submitted, open quotation can be edited."), frappe.ValidationError)
	if doc.status in NON_EDITABLE_STATUSES:
		frappe.throw(_("Quotation {0} is {1} and can no longer be edited.").format(doc.name, doc.status), frappe.ValidationError)


def _amend_with_lines(doc, lines: list[dict]) -> dict:
	"""Cancel `doc` and resubmit an amended copy with `lines` — every rate is
	recomputed from the current approved price, not just the one line that changed."""
	items = _priced_items(doc.party_name, doc.custom_markup_pct, lines)

	original_name = doc.name
	doc.cancel()

	amended = frappe.copy_doc(doc)
	amended.amended_from = original_name
	amended.items = []
	for item in items:
		amended.append("items", item)
	amended.insert(ignore_permissions=True)
	amended.submit()

	return _serialize(amended)


@frappe.whitelist(methods=["GET"])
def list_quotations(dealer: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0):
	# Excludes cancelled quotations — an edited (Phase 12 amended) quotation leaves
	# its pre-edit version behind as docstatus=2, which is history, not a live document.
	limit, offset = clamp(limit, offset)
	filters = {"quotation_to": "Customer", "docstatus": ["!=", 2]}
	if dealer:
		filters["party_name"] = dealer
	if search:
		filters["name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Quotation", filters=filters)
	names = frappe.get_all(
		"Quotation", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Quotation", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_quotation(quotation: str):
	return _serialize(frappe.get_doc("Quotation", quotation))


@frappe.whitelist(methods=["POST"])
def create_quotation(
	dealer: str,
	lines: list[dict],
	markup_pct: float,
	freight: float = 0,
	validity_days: int = 7,
	inquiries: list[str] | None = None,
	channel: str | None = None,
	taxes_and_charges: str | None = None,
):
	"""Each line in `lines` may carry `discount_percentage` (0-100) and/or
	`delivery_date` -- see _priced_items. `taxes_and_charges` names an existing
	Sales Taxes and Charges Template; see sales.utils.apply_tax_template. Both
	carry forward automatically into the resulting Sales Order on
	convert_to_order, via ERPNext's own make_sales_order field mapper.

	`inquiries` closes out every Inquiry passed (status -> "Quoted",
	linked_quotation -> this quotation's name, mirroring order_api's
	linked_sales_order pattern in the opposite direction) -- lets several
	inquiries for the same dealer merge into one quotation, not just one. Every
	inquiry must belong to `dealer`: a Quotation has exactly one party, so a
	mixed-dealer selection can't be merged and is rejected outright rather than
	silently dropped or split. custom_inquiry (a single Link, unchanged) keeps
	pointing at the first inquiry in the list, for existing single-inquiry
	callers/reports."""
	_assert_can_manage_quotations()

	if not lines:
		frappe.throw(_("At least one line is required."), frappe.ValidationError)
	if inquiries:
		mismatched = [
			i for i in inquiries if frappe.db.get_value("Inquiry", i, "dealer") != dealer
		]
		if mismatched:
			frappe.throw(
				_("{0} do not belong to {1} -- a quotation can only merge inquiries from one dealer.").format(
					", ".join(mismatched), dealer
				),
				frappe.ValidationError,
			)
	# BRD C.4.3: an explicit channel (including "Retail") is the audit-locked manual
	# override and always wins; only an unset channel triggers auto-classification.
	if channel is None:
		channel = auto_classify_channel(dealer, lines)
	elif channel not in ORDER_CHANNELS:
		frappe.throw(_("Invalid channel: {0}").format(channel), frappe.ValidationError)

	items = _priced_items(dealer, markup_pct, lines)

	doc = frappe.get_doc(
		{
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": dealer,
			"company": default_company(),
			"transaction_date": today(),
			"valid_till": add_days(today(), int(validity_days)),
			"custom_markup_pct": markup_pct,
			"custom_freight": freight,
			"custom_inquiry": inquiries[0] if inquiries else None,
			"custom_order_channel": channel,
			"items": items,
		}
	)
	apply_tax_template(doc, taxes_and_charges)
	doc.insert(ignore_permissions=True)
	clear_unrequested_default_tax(doc)
	doc.submit()

	for inquiry in inquiries or []:
		frappe.db.set_value("Inquiry", inquiry, {"status": "Quoted", "linked_quotation": doc.name})

	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def add_quotation_line(quotation: str, item: str, qty: float):
	_assert_can_manage_quotations()
	doc = frappe.get_doc("Quotation", quotation)
	_guard_editable(doc)

	if any(row.item_code == item for row in doc.items):
		frappe.throw(_("{0} is already a line on this quotation — use update_quotation_line_qty.").format(item), frappe.ValidationError)

	lines = _lines_from_items(doc.items) + [{"item": item, "qty": qty}]
	return _amend_with_lines(doc, lines)


@frappe.whitelist(methods=["POST"])
def remove_quotation_line(quotation: str, item: str):
	_assert_can_manage_quotations()
	doc = frappe.get_doc("Quotation", quotation)
	_guard_editable(doc)

	lines = [l for l in _lines_from_items(doc.items) if l["item"] != item]
	if not lines:
		frappe.throw(_("A quotation must have at least one line — cancel it instead of removing the last one."), frappe.ValidationError)
	if len(lines) == len(doc.items):
		frappe.throw(_("{0} is not a line on this quotation.").format(item), frappe.ValidationError)

	return _amend_with_lines(doc, lines)


@frappe.whitelist(methods=["POST", "PUT"])
def update_quotation_line_qty(quotation: str, item: str, qty: float):
	_assert_can_manage_quotations()
	doc = frappe.get_doc("Quotation", quotation)
	_guard_editable(doc)

	if not any(row.item_code == item for row in doc.items):
		frappe.throw(_("{0} is not a line on this quotation.").format(item), frappe.ValidationError)

	lines = _lines_from_items(doc.items)
	for l in lines:
		if l["item"] == item:
			l["qty"] = qty
	return _amend_with_lines(doc, lines)


@frappe.whitelist(methods=["POST", "PUT"])
def update_quotation_status(quotation: str, status: str, lost_reasons: list[str] | None = None, detailed_reason: str | None = None):
	"""Lost is the only status this sets directly — every other status (Ordered,
	the amend-driven states, etc.) is a side effect of some other action, not
	something a caller sets by hand. Reuses ERPNext's own `declare_enquiry_lost`
	rather than writing `status` on a submitted document ourselves; that's the
	native, safe way this doctype supports a manual post-submit status change.

	`declare_enquiry_lost` is a whitelisted Quotation document method, not a
	standalone importable function — it must be called on a loaded doc instance,
	with each reason as a {"lost_reason": ...} dict matching an existing
	Quotation Lost Reason record, not a plain string."""
	_assert_can_manage_quotations()

	if status != "Lost":
		frappe.throw(
			_("Only 'Lost' can be set directly — other statuses follow from create/amend/convert_to_order."),
			frappe.ValidationError,
		)

	doc = frappe.get_doc("Quotation", quotation)
	doc.declare_enquiry_lost(
		lost_reasons_list=[{"lost_reason": reason} for reason in (lost_reasons or [])],
		competitors=[],
		detailed_reason=detailed_reason,
	)
	return get_quotation(quotation)


@frappe.whitelist(methods=["POST"])
def convert_to_order(quotation: str, expected_dispatch=None):
	from erpnext.selling.doctype.quotation.quotation import make_sales_order

	from dms_erp.sales.order_api import finalize_new_order

	_assert_can_manage_quotations()

	qtn = frappe.get_doc("Quotation", quotation)
	so = make_sales_order(quotation)
	# make_sales_order carries taxes_and_charges/taxes across from the quotation
	# verbatim -- if the quotation was itself untaxed, this new Sales Order's taxes
	# table starts empty too, and is just as exposed to ERPNext's own validate()-time
	# default-template auto-population as a directly-created order. See
	# sales.utils.apply_tax_template's docstring.
	if not so.get("taxes_and_charges"):
		so.flags.dont_auto_add_taxes = True
	if expected_dispatch:
		so.delivery_date = expected_dispatch
		# Only fills the gap for a line that never had its own delivery_date on
		# the Quotation -- a line the dealer already committed to a specific date
		# for shouldn't get silently overwritten just because the order-level
		# date was set.
		for row in so.items:
			if not row.delivery_date:
				row.delivery_date = expected_dispatch

	order = finalize_new_order(so, source_type="Quotation", source_ref=quotation, channel=qtn.custom_order_channel)

	if qtn.custom_inquiry:
		frappe.db.set_value(
			"Inquiry", qtn.custom_inquiry, {"status": "Converted to Order", "linked_sales_order": order["id"]}
		)

	return order
