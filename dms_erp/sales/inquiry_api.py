"""Inquiries (BRD §4) — the demand-signal log a dealer conversation starts from.
No ERPNext doctype models "logged demand, not yet a sales document, that later
converts to a Quotation/Order or gets marked as missed" with Pacific's exact 10-state
status lifecycle, so this is a genuine custom doctype.

`convert_to_purchase_requirement` (Phase 12) is the missing piece of that 10-state
lifecycle: nothing ever set an Inquiry to "Mapped to PO" because nothing ever raised
a Purchase Order *from* one. It's a thin wrapper over `purchase.po_api.
create_purchase_order` (which still does the actual work, and still enforces its own
Purchase/Management role gate) — not a parallel "purchase requirement" doctype,
since a requirement here is just a PO with a `custom_source_inquiry` link back. Its
own `supplier` param is optional too (BRD D.2), just forwarded straight through to
create_purchase_order's own item/Series default-supplier fallback.

`create_inquiry` (Phase 14) used to enforce the same catalog gate `quotation_api.
create_quotation` always has — dealer-assigned visibility AND current sellability.
ISS-2026-00644 dropped the assigned-visibility half of that for inquiries
specifically: a dealer can now ask about (and WhatsApp/the dealer portal will show)
an item Purchase hasn't assigned to their Dealer Catalog yet — that's the whole
point of "inquire-only" for an unassigned item; only *ordering* it still requires
assignment (dealer_portal_api.convert_to_order, comms.flow_api's WhatsApp
order-placement path, and quotation_api.create_quotation all still gate on
dealer_catalog_api.is_visible/catalog_for, unchanged). A Pulled Back/discontinued
item is still rejected here either way — that part of the gate stays.

BRD C.2.4 same-moment duplicate check: create_inquiry (Phase 21) now looks for a
still-open inquiry for the same dealer+item within the last
sales.utils.DUPLICATE_INQUIRY_WINDOW_DAYS (the same rule and window
reports.duplicate_inquiry_report already used) and returns it as `duplicateOf` on
the response — a hint, not a block, so the caller decides whether to warn and let
the user confirm-and-continue, matching the BRD's own phrasing.

list_inquiries is paginated (`limit`/`offset`) and returns `{"items", "total",
"limit", "offset"}`, not a bare list -- reports need the whole result set, so
they call list_all_inquiries (unpaginated, internal-only) instead.

ISS-2026-00636 / MOM 3.6 — "Custom Style": `item` is no longer always required.
A dealer asking (today, only over WhatsApp — comms.flow_api's new
create_custom_style_inquiry) for a design not in the item master at all passes
`requested_style` (free text) instead of `item` — explicitly NOT an ERPNext
Opportunity and NOT a placeholder Item record, just this one extra field on the
same Inquiry doctype every other source already uses, so staff still work one
list regardless of how the request came in. Every item-shaped piece of
_create_inquiry (sellability, duplicate check, stock-derived status, the
Opportunity mirror) is skipped for one of these; ordering one is a contradiction
in terms (there's no real item yet), so dealer_portal_api.convert_to_order
rejects it explicitly rather than failing through an item-shaped code path with
a None item code.
"""

import json
import math

import frappe
from frappe import _

from dms_erp.catalog.utils import (
	convert_qty_to_boxes,
	is_sellable,
	item_pieces_per_box,
	item_sqft_per_box,
	item_sqm_per_box,
	item_weight_per_box_kg,
	status_label,
)
from dms_erp.pagination import clamp
from dms_erp.pricing.api import get_price_for_dealer
from dms_erp.sales.utils import find_open_duplicate_inquiries
from dms_erp.warehouse.utils import default_company, total_stock_for_item

INQUIRY_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
PURCHASE_REQUIREMENT_STATUSES = {"Open", "Out of Stock", "Pre-order Required"}


DEFAULT_DROP_REASONS = ["Price too high", "Bought elsewhere", "Not available in time", "Dealer not responding", "Requirement cancelled", "Other"]


def drop_reasons() -> list[str]:
	"""BRD C.2.3 -- "dropped inquiries record a reason". The list is per-site config (DMS
	Sales Settings, one per line) since this app is white-labeled; "Other" is always
	offered last, with its own free-text details."""
	configured = frappe.db.get_single_value("DMS Sales Settings", "inquiry_drop_reasons") or ""
	reasons = [line.strip() for line in configured.splitlines() if line.strip()] or list(DEFAULT_DROP_REASONS)
	return [r for r in reasons if r != "Other"] + ["Other"]


@frappe.whitelist(methods=["GET"])
def list_inquiry_drop_reasons() -> list[str]:
	_assert_can_manage_inquiries()
	return drop_reasons()


def _assert_can_manage_inquiries():
	if not set(frappe.get_roles(frappe.session.user)) & INQUIRY_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage inquiries."), frappe.PermissionError)


def _serialize(doc) -> dict:
	# ISS-2026-00636 -- a Custom Style request (see _create_inquiry) has no real
	# Item behind it at all; every one of these per-item lookups would either
	# error or return nothing meaningful against a None item code, so they're
	# skipped entirely rather than guarded field-by-field below.
	has_item = bool(doc.item)
	weight_per_box_kg = item_weight_per_box_kg(doc.item) if has_item else None
	pieces_per_box = item_pieces_per_box(doc.item) if has_item else None
	sqft_per_box = item_sqft_per_box(doc.item) if has_item else None
	sqm_per_box = item_sqm_per_box(doc.item) if has_item else None
	return {
		"id": doc.name,
		"number": doc.name,
		"date": doc.date,
		"createdAt": doc.creation,
		"dealerId": doc.dealer,
		"productId": doc.item,
		# ISS-2026-00636 -- the free-text design/style name a dealer typed through
		# WhatsApp's "Custom Style" option, when this inquiry has no real item
		# (productId is null); null for every ordinary, item-backed inquiry.
		"requestedStyle": doc.get("custom_requested_style"),
		# Several items raised together from the dealer portal share this id.
		"requestGroup": doc.get("request_group"),
		"qty": doc.qty,
		# Client request, 2 Nov 2026 -- the qty/unit as entered, when it wasn't Box
		# (see _create_inquiry). Null for a plain box inquiry -- nothing extra to show.
		# The truthy check (not just "is not None") also covers rows saved before
		# _create_inquiry stopped passing explicit None for these -- Frappe had been
		# coercing that into 0.0/"Box" rather than leaving them genuinely empty.
		"enteredQty": doc.get("entered_qty") or None,
		"enteredUom": doc.get("entered_uom") if doc.get("entered_qty") else None,
		# Client report, 2 Nov 2026 -- the Boxes component of a combined Boxes+Pcs
		# entry (see _create_inquiry) -- 0 is a real value here ("no boxes, just
		# pieces"), so this is only suppressed alongside enteredQty/enteredUom, not
		# on its own falsiness.
		"enteredBoxes": doc.get("entered_boxes") if doc.get("entered_qty") else None,
		"damagedStock": bool(doc.get("damaged_stock")),
		# ISS-2026-00712 -- the dealer's own batch choice, if any; None/"Auto" means
		# convert_to_order leaves it to order_api._resolve_delivery_picks at dispatch.
		"preferredBatchNo": doc.get("preferred_batch_no"),
		# BRD C.2.1/C.7.1 -- price derived from the dealer's own classification tier
		# (Standard/Dealer/Master), the same lookup Quotation/Order already use
		# (pricing.api.get_price_for_dealer), shown here at Inquiry stage too rather
		# than only once it becomes a Quotation. Purchase-history-based adjustment
		# within a tier doesn't exist anywhere in this app yet -- this is the tier
		# rate, not a per-dealer-adjusted one; that's a bigger, separate gap.
		"price": get_price_for_dealer(doc.item, doc.dealer) if has_item else None,
		"weightPerBoxKg": weight_per_box_kg,
		"totalWeightKg": (weight_per_box_kg or 0) * doc.qty if weight_per_box_kg is not None else None,
		"piecesPerBox": pieces_per_box,
		"totalPieces": (pieces_per_box or 0) * doc.qty if pieces_per_box is not None else None,
		"sqftPerBox": sqft_per_box,
		"totalSqft": (sqft_per_box or 0) * doc.qty if sqft_per_box is not None else None,
		"sqmPerBox": sqm_per_box,
		"totalSqm": round((sqm_per_box or 0) * doc.qty, 4) if sqm_per_box is not None else None,
		"status": doc.status,
		"source": doc.source,
		"expectedDelivery": doc.expected_delivery,
		"followUpDate": doc.follow_up_date,
		# Client request, 28 Oct 2026 -- the dealer portal's own "Ask for quotation"
		# button on an already-raised enquiry (dealer_portal_api.request_quotation).
		"dealerRequestedQuote": bool(doc.get("dealer_requested_quote")),
		"dealerRequestedQuoteAt": doc.get("dealer_requested_quote_at"),
		"assignedTo": doc.assigned_to,
		"remarks": doc.remarks,
		# BRD client req 8.9 -- which of the dealer's own sales staff phoned this in,
		# so several of a dealer's people inquiring about different items at once stay
		# distinguishable. Free text (no dealer-contacts master exists).
		"contactName": doc.contact_name,
		"contactPhone": doc.contact_phone,
		"whatsappReplied": bool(doc.whatsapp_replied),
		"customerPo": doc.customer_po,
		"dropReason": doc.get("drop_reason"),
		"dropReasonNote": doc.get("drop_reason_note"),
		"linkedSalesOrder": doc.linked_sales_order,
		"linkedQuotation": doc.linked_quotation,
		"linkedOpportunity": doc.linked_opportunity,
	}


def _mirror_to_opportunity(doc) -> str | None:
	"""7.9 -- every Inquiry, whatever its source (frontend, dealer portal,
	WhatsApp, staff -- every real creation path runs through _create_inquiry,
	this function's only caller), should be visible in ERPNext's native
	Opportunity list so desk staff have one list to work from instead of a
	separate native Inquiry-style list. One-way, create-time mirror only: the
	native "Create Quotation" button on the Opportunity is deliberately left
	unwired -- dms_erp's own Quotation flow (quotation_api.py) remains the real
	one. Later Inquiry status changes (Quoted/Converted to Order/Rejected/etc.)
	are NOT pushed back to the Opportunity -- every one of those happens via
	frappe.db.set_value elsewhere (quotation_api.py, order_api.py, this
	module's own convert_to_purchase_requirement), which bypasses Document
	hooks entirely, so keeping the two in lockstep would mean touching every
	one of those call sites -- a separate, bigger piece of work than what was
	asked for here.

	Best-effort: a dealer or staff member raising an Inquiry must never fail
	because this secondary, visibility-only mirror couldn't be created."""
	try:
		opportunity = frappe.get_doc(
			{
				"doctype": "Opportunity",
				"opportunity_from": "Customer",
				"party_name": doc.dealer,
				"company": default_company(),
				"transaction_date": doc.date,
				"custom_dms_inquiry": doc.name,
				# No pricing decision has been made at Inquiry stage — rate 0 is a
				# placeholder, not a real quote; Opportunity Item's rate/amount are
				# reqd=1 on the child table, so an explicit 0 (not just an unset
				# field) is required to pass validation.
				"items": [{"item_code": doc.item, "qty": doc.qty, "rate": 0}],
			}
		)
		opportunity.insert(ignore_permissions=True)
		return opportunity.name
	except Exception:
		frappe.log_error(title="Failed to mirror Inquiry to Opportunity", message=frappe.get_traceback())
		return None


def _inquiry_filters(dealer: str | None, status: str | None, search: str | None) -> dict:
	filters = {}
	if dealer:
		filters["dealer"] = dealer
	if status:
		filters["status"] = status
	if search:
		# ISS-2026-00636 -- a Custom Style inquiry has no item, only the free-text
		# custom_requested_style the dealer typed, so search has to look there too or
		# those inquiries could never be found by what was asked for. A dict filter
		# can't OR two fields (and frappe.db.count takes no or_filters), hence the
		# name-set union -- same pattern as order_api.list_orders' search.
		pattern = ["like", f"%{search}%"]
		matches = set(frappe.get_all("Inquiry", filters={"item": pattern}, pluck="name")) | set(
			frappe.get_all("Inquiry", filters={"custom_requested_style": pattern}, pluck="name")
		)
		filters["name"] = ["in", list(matches) or [""]]
	return filters


def list_all_inquiries(dealer: str | None = None, status: str | None = None, search: str | None = None) -> list[dict]:
	"""Unpaginated -- for internal callers (reports) that need the full result set,
	not a page of it. list_inquiries (the whitelisted endpoint) is the paginated one."""
	filters = _inquiry_filters(dealer, status, search)
	names = frappe.get_all("Inquiry", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Inquiry", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_inquiries(
	dealer: str | None = None, status: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0
):
	limit, offset = clamp(limit, offset)
	filters = _inquiry_filters(dealer, status, search)
	total = frappe.db.count("Inquiry", filters=filters)
	names = frappe.get_all(
		"Inquiry", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Inquiry", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_inquiry(inquiry: str):
	return _serialize(frappe.get_doc("Inquiry", inquiry))


@frappe.whitelist(methods=["GET"])
def default_addresses_for_dealer(dealer: str) -> dict:
	"""Gap-sheet item #1 (native-Desk audit, 6 Nov 2026) -- inquiry.js's own
	"Create Quotation"/"Create Order" Desk dialogs call quotation_api.
	create_quotation/order_api.create_order directly, but neither dialog ever
	collected customer_address/shipping_address_name -- both mandatory
	(BRD C.3.3), so every single Desk-created Quotation/Order failed outright
	with "A bill-to (billing) address is required." the instant "Create" was
	clicked. This is the exact same "no address-picker UI" gap every other
	self-service channel already has a fallback for
	(sales.utils.default_billing_shipping_for) -- just a thin whitelisted
	wrapper so the Desk dialogs (which have no business calling a private
	utility function directly) can resolve the same sane default before
	calling create_quotation/create_order, instead of guessing an address or
	skipping the fields entirely."""
	from dms_erp.sales.utils import default_billing_shipping_for

	billing, shipping = default_billing_shipping_for(dealer)
	return {"customerAddress": billing, "shippingAddressName": shipping}


@frappe.whitelist()
def make_quotation(source_name: str, target_doc=None):
	""""Create > Quotation" on an Inquiry (6 Nov 2026 -- the user explicitly asked
	twice not to put a custom dialog in front of this, pointing at ERPNext's own
	native Opportunity -> Quotation as the model to match) -- this is that exact
	mechanism: `frappe.model.mapper.get_mapped_doc`, the same mapper
	erpnext.crm.doctype.opportunity.opportunity.make_quotation uses, called from
	inquiry.js via `frappe.model.open_mapped_doc` (no dialog at all -- it routes
	straight to a new, unsaved Quotation form pre-filled from this Inquiry, same
	as clicking Opportunity's own "Create Quotation").

	Markup %/Freight start at 0 -- not asked for up front, just editable on the
	form like any other field, with price_list_rate recomputed live as Markup %
	changes (quotation_api.price_preview, called by the Quotation Client Script,
	sales/setup.py) so the number on screen is real, not a placeholder.

	This can NOT reuse quotation_api._priced_items/create_quotation's one-shot
	create+submit: the whole point here is an editable draft the user can change
	before saving, and _priced_items throws immediately on the first catalog/
	stock/sellability problem it finds rather than letting a draft exist at all.
	Real validation happens once, at Submit (hooks.py's Quotation before_submit
	-> quotation_api.guard_native_submit) -- the one place every path through
	this doctype (native or API) is guaranteed to pass through.

	`target_doc` (optional) is this same function doubling as the mapper behind
	Quotation's own "Get Items From > Inquiry" button (sales/setup.py's
	Quotation Client Script, erpnext.utils.map_current_doc) -- staff merging
	several open Inquiries into one Quotation draft natively, same capability
	quotation_api.create_quotation's own `inquiries` list already has, just
	reachable from the Quotation side instead of needing one button-click per
	Inquiry. frappe.model.mapper.map_docs calls this once per Inquiry selected,
	threading the previous call's returned (still-unsaved) doc back in as
	`target_doc` -- so document-level fields (party_name/addresses/channel/etc.)
	are only ever set once, on the first Inquiry in the batch (target.party_name
	still empty); every later call in the same batch just appends another item
	row onto the same draft. A later Inquiry for a different dealer than the
	first is rejected outright -- same one-dealer-per-quotation rule
	create_quotation's own `inquiries` merge already enforces, just checked
	here instead since nothing else would catch it before Submit."""
	from frappe.model.mapper import get_mapped_doc

	from dms_erp.sales.order_channel import auto_classify_channel
	from dms_erp.sales.utils import default_billing_shipping_for

	inquiry = frappe.get_doc("Inquiry", source_name)
	if not inquiry.item:
		frappe.throw(
			_("{0} is a custom style request with no catalog item -- it can't become a Quotation.").format(source_name),
			frappe.ValidationError,
		)

	existing = json.loads(target_doc) if isinstance(target_doc, str) else target_doc
	existing_dealer = existing.get("party_name") if existing else None
	if existing_dealer and existing_dealer != inquiry.dealer:
		frappe.throw(
			_("{0} belongs to {1}, not {2} -- a quotation can only merge inquiries from one dealer.").format(
				source_name, inquiry.dealer, existing_dealer
			),
			frappe.ValidationError,
		)

	customer_address, shipping_address_name = default_billing_shipping_for(inquiry.dealer)
	price_list_rate = get_price_for_dealer(inquiry.item, inquiry.dealer) or 0
	item_name, stock_uom = frappe.db.get_value("Item", inquiry.item, ["item_name", "stock_uom"])

	def set_missing_values(source, target):
		if not target.party_name:
			target.quotation_to = "Customer"
			target.party_name = source.dealer
			target.transaction_date = frappe.utils.today()
			target.valid_till = frappe.utils.add_days(frappe.utils.today(), 7)
			target.company = default_company()
			target.custom_markup_pct = 0
			target.custom_freight = 0
			target.custom_order_channel = auto_classify_channel(source.dealer, [{"item": source.item, "qty": source.qty}])
			target.customer_address = customer_address
			target.shipping_address_name = shipping_address_name
		target.custom_inquiry = target.custom_inquiry or source.name
		target.append(
			"items",
			{
				"item_code": source.item,
				# Item Name/UOM/Stock UOM/Conversion Factor -- normally filled in by the
				# browser's own item_code change handler (get_item_details) the moment
				# staff picks an item by hand; a mapped row never fires that, so Desk's
				# own client-side mandatory-field check ("Missing Fields": Item Name,
				# UOM) failed on Save before the request even reached the server, even
				# though server-side validate() would have backfilled them anyway.
				"item_name": item_name,
				"uom": stock_uom,
				"stock_uom": stock_uom,
				"conversion_factor": 1,
				"qty": source.qty,
				"price_list_rate": price_list_rate,
				"rate": price_list_rate,
			},
		)

	return get_mapped_doc(
		"Inquiry",
		source_name,
		{
			"Inquiry": {
				"doctype": "Quotation",
				# get_mapped_doc auto-copies any identically-named field (frappe/model/
				# mapper.py's map_fields) -- Inquiry.source ("WhatsApp"/"Web", free text)
				# and Quotation.source (a Link to the CRM "Source" master) collide by pure
				# coincidence and mean something completely different; copying Inquiry's
				# value across broke every WhatsApp-sourced Inquiry's mapped doc outright
				# with "Could not find Source: WhatsApp" the moment it was inserted.
				# Inquiry.status/Quotation.status collide the same way (10-state Inquiry
				# lifecycle vs. Quotation's own status), excluded defensively alongside it.
				"field_no_map": ["source", "status"],
			}
		},
		target_doc=target_doc,
		postprocess=set_missing_values,
	)


@frappe.whitelist()
def make_order(source_name: str):
	""""Create > Order" on an Inquiry -- same native mapped-doc pattern as
	make_quotation above (6 Nov 2026, same ask), for the direct Inquiry ->
	Sales Order path (no Quotation, no retail markup -- order_api.create_order's
	own direct path, same plain approved-dealer-price rate).

	order_api.finalize_new_order (the one every create_order call goes
	through) stamps `custom_fulfillment_stage`/`custom_stage_history` at
	creation time -- a mapped draft never goes through it, so
	order_api.guard_native_submit (this doctype's own before_submit hook)
	stamps that bookkeeping itself, once, right before Submit, the same way it
	validates addresses/discount/channel there instead of here for the same
	"editable draft, can't throw on open" reason make_quotation's docstring
	explains."""
	from frappe.model.mapper import get_mapped_doc

	from dms_erp.sales.order_channel import auto_classify_channel
	from dms_erp.sales.utils import default_billing_shipping_for

	inquiry = frappe.get_doc("Inquiry", source_name)
	if not inquiry.item:
		frappe.throw(
			_("{0} is a custom style request with no catalog item -- it can't become an Order.").format(source_name),
			frappe.ValidationError,
		)

	customer_address, shipping_address_name = default_billing_shipping_for(inquiry.dealer)
	price_list_rate = get_price_for_dealer(inquiry.item, inquiry.dealer) or 0
	item_name, stock_uom = frappe.db.get_value("Item", inquiry.item, ["item_name", "stock_uom"])

	def set_missing_values(source, target):
		target.customer = source.dealer
		target.transaction_date = frappe.utils.today()
		target.company = default_company()
		target.po_no = source.customer_po
		target.custom_source_type = "Inquiry"
		target.custom_source_ref = source.name
		target.custom_order_channel = auto_classify_channel(source.dealer, [{"item": source.item, "qty": source.qty}])
		target.customer_address = customer_address
		target.shipping_address_name = shipping_address_name
		target.append(
			"items",
			{
				"item_code": source.item,
				# See make_quotation's own comment on item_name/uom/stock_uom/
				# conversion_factor -- same client-side "Missing Fields" issue, same fix.
				"item_name": item_name,
				"uom": stock_uom,
				"stock_uom": stock_uom,
				"conversion_factor": 1,
				"qty": source.qty,
				"price_list_rate": price_list_rate,
				"rate": price_list_rate,
			},
		)

	return get_mapped_doc(
		"Inquiry",
		source_name,
		{
			"Inquiry": {
				"doctype": "Sales Order",
				# Same get_mapped_doc field-collision problem make_quotation hit --
				# Inquiry.source/status collide with Sales Order's own same-named
				# fields the same way they do with Quotation's; contact_phone also
				# collides here (Sales Order's is tied to a Contact record, Inquiry's
				# is free text) and isn't something this mapper sets anyway.
				"field_no_map": ["source", "status", "contact_phone"],
			}
		},
		postprocess=set_missing_values,
	)


def _create_inquiry(
	dealer: str,
	item: str | None,
	qty: float,
	source: str,
	expected_delivery=None,
	follow_up_date=None,
	assigned_to: str | None = None,
	remarks: str | None = None,
	contact_name: str | None = None,
	contact_phone: str | None = None,
	requested_style: str | None = None,
	request_group: str | None = None,
	damaged_stock: bool = False,
	uom: str | None = None,
	entered_boxes: float | None = None,
	preferred_batch_no: str | None = None,
) -> dict:
	"""Unguarded core of create_inquiry -- also called directly by
	sales.dealer_portal_api.raise_inquiry, whose own DMS Dealer session (scoped to
	its own dealer identity, never a caller-supplied one) is the authorization for
	that path, not INQUIRY_WRITE_ROLES.

	BRD C.1.3/C.2.2, client request 2 Nov 2026 -- `uom` (Box/Pcs/Sqft/Sqm/Kg/Gram,
	catalog.utils.ORDER_UOMS) lets a dealer enter qty in whatever unit they think
	in, same as order_api.create_order/quotation_api.create_quotation already do.
	`qty` is converted to its box-equivalent via catalog.utils.convert_qty_to_boxes
	before anything below (stock comparison, the duplicate check, the stored doc,
	the Opportunity mirror) ever sees it -- every one of those operates on boxes
	only, same as the two order-side entry points. The originally entered qty/unit
	survive on entered_qty/entered_uom purely for display. Not applicable to a
	Custom Style request (no Item to look up a per-box figure against), so `uom`
	is ignored when `item` is None -- same item-only branching the rest of this
	function already uses.

	Client report, 2 Nov 2026 -- `entered_boxes`, when given, means the dealer
	portal's own combined Boxes+Pcs entry: `qty` here is the Pcs component only,
	`entered_boxes` the Boxes component, and the two combine into one box-equivalent
	(the Pcs component rounds up to a whole box, same convention convert_qty_to_boxes
	uses). Takes priority over plain `uom` conversion when both are somehow given.
	Deliberately NOT collapsed into a single "total pieces" figure before combining
	-- doing that first and only then splitting it back into entered_boxes/entered_qty
	for display does NOT round-trip to what the dealer actually typed whenever the
	Pcs component is at or above the item's own pieces-per-box figure (50 Box + 5 Pcs
	on a 4-pcs box is 52 boxes total, but re-deriving from 205 combined pieces gives
	back "51 Box 1 Pcs", not "50 Box 5 Pcs") -- a real bug this fixes.

	ISS-2026-00678 -- `damaged_stock` marks this as the dealer explicitly asking
	for damage/insurance-claim bay stock specifically (shown separately on the
	portal, see warehouse.utils.excluded_stock_for_item), not regular inventory.
	Carries through unchanged to Quotation Item/Sales Order Item as
	custom_damaged_stock (same fieldname both sides, so ERPNext's native
	Quotation->Sales Order mapper copies it with no extra glue) so picking routes
	to the Damages Bay instead of a normal one.

	ISS-2026-00712 -- `preferred_batch_no` is the dealer's own batch choice at
	enquiry time (ignored for a Custom Style request -- no real Item/stock to
	validate it against). Checked against real on-hand stock in that batch the
	same way order_api._priced_order_line checks it for a staff/dealer order line
	directly -- rejected immediately rather than only failing later if/when this
	enquiry is converted to an order. Carried onto the resulting Sales Order line
	by dealer_portal_api.convert_to_order if/when that happens; order_api.
	_resolve_delivery_picks re-checks it again at dispatch time regardless, since
	stock can move between enquiry and dispatch.

	ISS-2026-00636 / MOM 3.6 -- "Custom Style": exactly one of `item`/
	`requested_style` is given, never both, never neither. `requested_style` is a
	free-text design name with no Item behind it at all (comms.flow_api's new
	"Custom Style" WhatsApp option is the only caller today) -- deliberately
	NOT an ERPNext Opportunity and not a placeholder Item record, just this one
	extra field on the same Inquiry every other source already creates, so staff
	work the one list regardless of how the request came in. Every per-item
	computation below (sellability, duplicate check, stock-derived status,
	Opportunity mirror) only makes sense against a real Item, so the whole
	item-shaped half of this function is skipped for a Custom Style request."""
	if bool(item) == bool(requested_style):
		frappe.throw(_("Exactly one of item or requested_style is required."), frappe.ValidationError)

	entered_qty = None
	entered_uom = None
	stored_entered_boxes = None
	if item and entered_boxes is not None:
		pieces = qty
		boxes = entered_boxes
		if pieces > 0:
			ppb = item_pieces_per_box(item)
			if not ppb:
				frappe.throw(
					_("{0} has no Pcs-per-box figure set, so pieces can't be added to a box quantity for it.").format(item),
					frappe.ValidationError,
				)
			qty = boxes + math.ceil(pieces / ppb)
			entered_qty = pieces
			entered_uom = "Pcs"
			stored_entered_boxes = boxes
		else:
			qty = boxes
	elif item and uom and uom != "Box":
		entered_qty = qty
		entered_uom = uom
		qty = convert_qty_to_boxes(item, qty, uom)

	if assigned_to is None and set(frappe.get_roles(frappe.session.user)) & INQUIRY_WRITE_ROLES:
		# Only a staff caller defaults to self-assignment -- a dealer-portal session
		# (raise_inquiry) has no INQUIRY_WRITE_ROLES membership, so its inquiries stay
		# unassigned rather than "assigned to" the dealer's own portal account.
		assigned_to = frappe.session.user

	duplicates = []
	if item:
		status = frappe.get_cached_value("Item", item, "custom_discontinuation_status") or "Active"
		if not is_sellable(status):
			frappe.throw(_("{0} is {1} and can no longer be quoted.").format(item, status_label(status)), frappe.ValidationError)

		# BRD C.2.4 — checked before insert, so the new inquiry can't match itself.
		duplicates = find_open_duplicate_inquiries(dealer, item)

		# ISS-2026-00678 -- a damaged-stock request's "on hand" must be the
		# damage/insurance-claim bay total specifically, not total_stock_for_item
		# (which blends it with regular stock and would report "Available" even
		# when the Damages Bay alone can't actually cover qty). Deliberately
		# excludes "display" bay stock -- that's sample/display inventory with its
		# own Sample & Display Management workflow (catalog.sample_api), not stock
		# a dealer orders through an Inquiry.
		if damaged_stock:
			from dms_erp.warehouse.utils import DAMAGE_BAY_TYPES, excluded_stock_for_item

			on_hand = sum(row["boxes"] for row in excluded_stock_for_item(item) if row["bayType"] in DAMAGE_BAY_TYPES)
		else:
			# Derived from real on-hand qty rather than always "Open" -- this is what
			# feeds the reorder engine's missed-demand signal (purchase.reorder_api.
			# MISSED_DEMAND_STATUSES): nothing else ever sets an Inquiry to Out of Stock.
			# "Pre-order Required" has no stock-derivable signal of its own and stays a
			# staff call via update_inquiry.
			on_hand = total_stock_for_item(item)
		if on_hand <= 0:
			inquiry_status = "Out of Stock"
		elif on_hand >= qty:
			inquiry_status = "Available"
		else:
			inquiry_status = "Partially Available"
	else:
		# A Custom Style request has no stock/sellability concept to derive a
		# status from -- it starts "Open", exactly like a normal Inquiry would
		# before anyone looked at it, and moves on from there the same manual way
		# (update_inquiry) as "Pre-order Required" already does.
		inquiry_status = "Open"

	if preferred_batch_no and item:
		from dms_erp.warehouse.utils import all_batches

		available = next((b["boxes"] for b in all_batches(item) if b["batchNumber"] == preferred_batch_no), 0)
		if available < qty:
			frappe.throw(
				_("Batch {0} only has {1} boxes of {2} on hand -- not enough for the {3} requested.").format(
					preferred_batch_no, available, item, qty
				),
				frappe.ValidationError,
			)

	doc = frappe.get_doc(
		{
			"doctype": "Inquiry",
			"dealer": dealer,
			"item": item,
			"custom_requested_style": requested_style,
			"request_group": request_group,
			"qty": qty,
			# Client report, 2 Nov 2026 -- omitted entirely for a plain-box entry, same as
			# catalog.utils.normalize_order_line_uoms already does for Sales Order Item/
			# Quotation Item, rather than passed as explicit None -- Frappe coerces an
			# explicit None into the field's own "empty" value on save (0.0 for the Float
			# entered_qty, the Select's first listed option -- "Box" -- for entered_uom),
			# which is indistinguishable from a real "entered as 0 Box" and was showing as
			# exactly that on every plain-box enquiry.
			**(
				{"entered_boxes": stored_entered_boxes, "entered_qty": entered_qty, "entered_uom": entered_uom}
				if entered_uom
				else {}
			),
			"damaged_stock": damaged_stock,
			"preferred_batch_no": preferred_batch_no if item else None,
			"source": source,
			"status": inquiry_status,
			"expected_delivery": expected_delivery,
			"follow_up_date": follow_up_date,
			"assigned_to": assigned_to,
			"remarks": remarks,
			"contact_name": contact_name,
			"contact_phone": contact_phone,
		}
	)
	doc.insert(ignore_permissions=True)

	# A Custom Style request is explicitly never mirrored to an Opportunity (per
	# the ticket's own "not linked to Item master" intent) -- _mirror_to_opportunity
	# hardcodes a real item_code into Opportunity Item, which doesn't exist here.
	opportunity = _mirror_to_opportunity(doc) if item else None
	if opportunity:
		doc.db_set("linked_opportunity", opportunity, update_modified=False)

	result = _serialize(doc)
	result["duplicateOf"] = duplicates[0]["id"] if duplicates else None
	return result


@frappe.whitelist(methods=["POST"])
def create_inquiry(
	dealer: str,
	qty: float,
	source: str,
	item: str | None = None,
	expected_delivery=None,
	follow_up_date=None,
	assigned_to: str | None = None,
	remarks: str | None = None,
	contact_name: str | None = None,
	contact_phone: str | None = None,
	requested_style: str | None = None,
):
	_assert_can_manage_inquiries()
	return _create_inquiry(
		dealer,
		item,
		qty,
		source,
		expected_delivery,
		follow_up_date,
		assigned_to,
		remarks,
		contact_name,
		contact_phone,
		requested_style,
	)


@frappe.whitelist(methods=["POST", "PUT"])
def update_inquiry(inquiry: str, patch: dict):
	_assert_can_manage_inquiries()

	field_map = {
		"qty": "qty",
		"status": "status",
		"source": "source",
		"expectedDelivery": "expected_delivery",
		"followUpDate": "follow_up_date",
		"assignedTo": "assigned_to",
		"remarks": "remarks",
		"whatsappReplied": "whatsapp_replied",
		"contactName": "contact_name",
		"contactPhone": "contact_phone",
		"customerPo": "customer_po",
		"dropReason": "drop_reason",
		"dropReasonNote": "drop_reason_note",
	}

	doc = frappe.get_doc("Inquiry", inquiry)
	for key, value in patch.items():
		fieldname = field_map.get(key)
		if fieldname:
			doc.set(fieldname, value.strip() if isinstance(value, str) else value)

	# BRD C.2.3 -- closing without a customer PO is allowed (Pacific chose a warning, shown
	# by the staff app, not a block), but a drop reason that is given must be a real one.
	if doc.drop_reason:
		if doc.drop_reason not in drop_reasons():
			frappe.throw(_("{0} is not one of the inquiry drop reasons.").format(doc.drop_reason), frappe.ValidationError)
		if doc.drop_reason == "Other" and not (doc.drop_reason_note or "").strip():
			frappe.throw(_("Describe the reason when choosing Other."), frappe.ValidationError)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def convert_to_purchase_requirement(
	inquiry: str,
	expected_ready_date,
	supplier: str | None = None,
	ordered_qty: float | None = None,
	remarks: str | None = None,
):
	from dms_erp.purchase.po_api import create_purchase_order

	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.status not in PURCHASE_REQUIREMENT_STATUSES:
		frappe.throw(
			_("Inquiry {0} is {1} — only Open, Out of Stock or Pre-order Required inquiries can become a purchase requirement.").format(
				inquiry, doc.status
			),
			frappe.ValidationError,
		)
	if not doc.item:
		# ISS-2026-00636 -- a Custom Style inquiry has only free text, nothing to buy.
		# Without this, create_purchase_order fails on a None item with an error that
		# says nothing about why.
		frappe.throw(
			_("Inquiry {0} is a custom style request with no catalog item -- find or create the item first.").format(
				inquiry
			),
			frappe.ValidationError,
		)

	po = create_purchase_order(
		item=doc.item,
		ordered_qty=ordered_qty or doc.qty,
		supplier=supplier,
		expected_ready_date=expected_ready_date,
		remarks=remarks,
		source_inquiry=inquiry,
	)

	frappe.db.set_value("Inquiry", inquiry, "status", "Mapped to PO")
	return po
