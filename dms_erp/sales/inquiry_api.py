"""Inquiries (BRD §4) — the demand-signal log a dealer conversation starts from.
No ERPNext doctype models "logged demand, not yet a sales document, that later
converts to a Quotation/Order or gets marked as missed" with Pacific's exact
status lifecycle, so this is a genuine custom doctype.

12 Nov 2026 rebuild (BRD D.3, client request): one Inquiry now genuinely holds
every item the dealer asked about together, as real Inquiry Item child rows —
matching how Sales Order/Quotation already hold their own lines — instead of the
original one-Inquiry-per-item design, where several items raised together were
only ever N separate Inquiry records sharing a `request_group` string. That
workaround is gone; `request_group` no longer exists. `status` on the Inquiry
itself is now workflow-only (Open/Quoted/Converted to Order/Rejected/Mapped to
PO/Closed); per-item stock availability (Available/Partially Available/Out of
Stock/Pre-order Required) and `duplicate_flag` moved to each row's own Inquiry
Item, since one Inquiry's items can genuinely be in different stock positions at
once. `linked_sales_order`/`linked_quotation` stay on the Inquiry (one combined,
multi-line Quotation/Order per Inquiry now — see quotation_api.create_quotation/
order_api.create_order — not one per item).

`convert_to_purchase_requirement` (Phase 12) is the missing piece of the status
lifecycle: nothing ever set an Inquiry to "Mapped to PO" because nothing ever
raised a Purchase Order *from* one. It's a thin wrapper over `purchase.po_api.
create_purchase_order` (which still does the actual work, and still enforces its
own Purchase/Management role gate) — not a parallel "purchase requirement"
doctype, since a requirement here is just a PO with a `custom_source_inquiry`
link back. Its own `supplier` param is optional too (BRD D.2), just forwarded
straight through to create_purchase_order's own item/Series default-supplier
fallback. Since an Inquiry can hold several items now, `item` picks which row to
raise the PO for — required once there's more than one.

`create_inquiry` used to enforce the same catalog gate `quotation_api.
create_quotation` always has — dealer-assigned visibility AND current
sellability. ISS-2026-00644 dropped the assigned-visibility half of that for
inquiries specifically: a dealer can now ask about (and WhatsApp/the dealer
portal will show) an item Purchase hasn't assigned to their Dealer Catalog yet —
that's the whole point of "inquire-only" for an unassigned item; only *ordering*
it still requires assignment (dealer_portal_api.convert_to_order, comms.flow_api's
WhatsApp order-placement path, and quotation_api.create_quotation all still gate
on dealer_catalog_api.is_visible/catalog_for, unchanged). A Pulled Back/
discontinued item is still rejected here either way — that part of the gate
stays, per item.

BRD C.2.4 same-moment duplicate check: create_inquiry looks for a still-open
inquiry item for the same dealer+item within the last sales.utils.
DUPLICATE_INQUIRY_WINDOW_DAYS (the same rule and window reports.
duplicate_inquiry_report already used) and returns it as `duplicateOf` on the
response, per item — a hint, not a block, so the caller decides whether to warn
and let the user confirm-and-continue, matching the BRD's own phrasing.

list_inquiries is paginated (`limit`/`offset`) and returns `{"items", "total",
"limit", "offset"}`, not a bare list -- reports need the whole result set, so
they call list_all_inquiries (unpaginated, internal-only) instead.

ISS-2026-00636 / MOM 3.6 — "Custom Style": an item row's `item` is no longer
always required. A dealer asking (today, only over WhatsApp — comms.flow_api's
create_custom_style_inquiry) for a design not in the item master at all passes
`requested_style` (free text) instead — explicitly NOT an ERPNext Opportunity
and NOT a placeholder Item record, just that row's own extra field, so staff
work the one list regardless of how the request came in. Every item-shaped
computation per row (sellability, duplicate check, stock-derived availability,
the Opportunity mirror) only makes sense against a real Item, so each of those
is skipped for a Custom Style row specifically, not the whole Inquiry.
"""

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
from dms_erp.sales.utils import find_open_duplicate_inquiries, recent_inquiry_for
from dms_erp.warehouse.utils import default_company, total_stock_for_item

INQUIRY_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
# 12 Nov 2026 rebuild -- pre-rebuild this also listed the four stock-availability
# values (Available/Partially Available/Out of Stock/Pre-order Required), but those
# were always just sub-states of a still-pending Inquiry; the set never actually meant
# anything but "hasn't moved past Open yet" (Quoted/Converted to Order/Rejected/Mapped
# to PO/Closed were always excluded). Now that those stock states live on each Inquiry
# Item row instead of the parent, "Open" is the whole check.
PURCHASE_REQUIREMENT_STATUSES = {"Open"}


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


def _serialize_item_row(row) -> dict:
	"""One Inquiry Item child row, in the same per-item shape the old (one-record-
	per-item) _serialize used to return at the top level -- every existing caller
	that read productId/qty/etc off an inquiry now reads the same keys off one
	entry of its own `items` list instead."""
	has_item = bool(row.item)
	weight_per_box_kg = item_weight_per_box_kg(row.item) if has_item else None
	pieces_per_box = item_pieces_per_box(row.item) if has_item else None
	sqft_per_box = item_sqft_per_box(row.item) if has_item else None
	sqm_per_box = item_sqm_per_box(row.item) if has_item else None
	return {
		"lineId": row.name,
		"productId": row.item,
		"requestedStyle": row.get("custom_requested_style"),
		"qty": row.qty,
		"enteredQty": row.get("entered_qty") or None,
		"enteredUom": row.get("entered_uom") if row.get("entered_qty") else None,
		"enteredBoxes": row.get("entered_boxes") if row.get("entered_qty") else None,
		"damagedStock": bool(row.get("damaged_stock")),
		"preferredBatchNo": row.get("preferred_batch_no"),
		"availabilityStatus": row.get("availability_status") or "Open",
		"duplicateFlag": bool(row.get("duplicate_flag")),
		"weightPerBoxKg": weight_per_box_kg,
		"totalWeightKg": (weight_per_box_kg or 0) * row.qty if weight_per_box_kg is not None else None,
		"piecesPerBox": pieces_per_box,
		"totalPieces": (pieces_per_box or 0) * row.qty if pieces_per_box is not None else None,
		"sqftPerBox": sqft_per_box,
		"totalSqft": (sqft_per_box or 0) * row.qty if sqft_per_box is not None else None,
		"sqmPerBox": sqm_per_box,
		"totalSqm": round((sqm_per_box or 0) * row.qty, 4) if sqm_per_box is not None else None,
	}


def _serialize(doc) -> dict:
	items = [_serialize_item_row(row) for row in doc.items]
	return {
		"id": doc.name,
		"number": doc.name,
		"date": doc.date,
		"createdAt": doc.creation,
		"dealerId": doc.dealer,
		"items": items,
		"itemCount": len(items),
		# Price per row, keyed by the row's own lineId -- every caller that used to
		# read a bare top-level `price` off a single-item Inquiry now looks up its
		# item's own price here instead, same resolution (pricing.api.
		# get_price_for_dealer) as before, just moved alongside the rest of the
		# per-item data rather than duplicated inside each items[] entry.
		"prices": {
			row.name: get_price_for_dealer(row.item, doc.dealer) for row in doc.items if row.item
		},
		"status": doc.status,
		"source": doc.source,
		"expectedDelivery": doc.expected_delivery,
		"followUpDate": doc.follow_up_date,
		"dealerRequestedQuote": bool(doc.get("dealer_requested_quote")),
		"dealerRequestedQuoteAt": doc.get("dealer_requested_quote_at"),
		"assignedTo": doc.assigned_to,
		"remarks": doc.remarks,
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
	are NOT pushed back to the Opportunity.

	12 Nov 2026 rebuild: one Opportunity Item row per real-item Inquiry Item row
	(a Custom Style row has no item_code to mirror, so it's skipped -- same as
	the whole Inquiry used to be skipped outright when it was a lone Custom Style
	request). An Inquiry with no real items at all (every row a Custom Style)
	still gets no Opportunity, same as before.

	Best-effort: a dealer or staff member raising an Inquiry must never fail
	because this secondary, visibility-only mirror couldn't be created."""
	lines = [{"item_code": row.item, "qty": row.qty, "rate": 0} for row in doc.items if row.item]
	if not lines:
		return None
	try:
		opportunity = frappe.get_doc(
			{
				"doctype": "Opportunity",
				"opportunity_from": "Customer",
				"party_name": doc.dealer,
				"company": default_company(),
				"transaction_date": doc.date,
				"custom_dms_inquiry": doc.name,
				"items": lines,
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
		# 12 Nov 2026 rebuild -- item/custom_requested_style now live on the child
		# table, not the Inquiry itself, so a plain `filters["item"]` (the old
		# approach) can no longer match anything; find which parents have a
		# matching child row instead.
		pattern = ["like", f"%{search}%"]
		matches = set(frappe.get_all("Inquiry Item", filters={"item": pattern, "parenttype": "Inquiry"}, pluck="parent")) | set(
			frappe.get_all("Inquiry Item", filters={"custom_requested_style": pattern, "parenttype": "Inquiry"}, pluck="parent")
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
	""""Create > Quotation" on an Inquiry -- native mapped-doc pattern
	(`frappe.model.mapper.get_mapped_doc`, same as ERPNext's own native
	Opportunity -> Quotation), called from inquiry.js via
	`frappe.model.open_mapped_doc` -- routes straight to a new, unsaved
	Quotation form pre-filled from this Inquiry's own items.

	12 Nov 2026 rebuild: since one Inquiry already holds every item it needs
	(no more merging several separate Inquiries into one Quotation draft -- the
	old `target_doc` chaining this function used for that is gone, each
	real-item Inquiry Item row just becomes its own Quotation Item row), this is
	simpler than before: one Inquiry maps to exactly one Quotation draft, in one
	call. A Custom Style-only Inquiry (no real item on any row) has nothing to
	map and is rejected outright, same as before.

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
	this doctype (native or API) is guaranteed to pass through."""
	from frappe.model.mapper import get_mapped_doc

	from dms_erp.sales.order_channel import auto_classify_channel
	from dms_erp.sales.utils import default_billing_shipping_for

	inquiry = frappe.get_doc("Inquiry", source_name)
	real_rows = [row for row in inquiry.items if row.item]
	if not real_rows:
		frappe.throw(
			_("{0} has no catalog items (only custom style requests) -- it can't become a Quotation.").format(source_name),
			frappe.ValidationError,
		)

	customer_address, shipping_address_name = default_billing_shipping_for(inquiry.dealer)
	lines = [{"item": row.item, "qty": row.qty} for row in real_rows]

	def set_missing_values(source, target):
		target.quotation_to = "Customer"
		target.party_name = source.dealer
		target.transaction_date = frappe.utils.today()
		target.valid_till = frappe.utils.add_days(frappe.utils.today(), 7)
		target.company = default_company()
		target.custom_markup_pct = 0
		target.custom_freight = 0
		target.custom_order_channel = auto_classify_channel(source.dealer, lines)
		target.customer_address = customer_address
		target.shipping_address_name = shipping_address_name
		target.custom_inquiry = source.name
		target.items = []
		for row in real_rows:
			price_list_rate = get_price_for_dealer(row.item, source.dealer) or 0
			item_name, stock_uom = frappe.db.get_value("Item", row.item, ["item_name", "stock_uom"])
			target.append(
				"items",
				{
					"item_code": row.item,
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
					"qty": row.qty,
					"price_list_rate": price_list_rate,
					"rate": price_list_rate,
					"custom_damaged_stock": row.get("damaged_stock"),
					"custom_preferred_batch_no": row.get("preferred_batch_no"),
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
				# Inquiry.status/Quotation.status collide the same way (Inquiry lifecycle
				# vs. Quotation's own status), excluded defensively alongside it. "items"
				# excluded too -- set_missing_values builds Quotation Item rows itself
				# (different shape/fields than Inquiry Item), the default child-table
				# auto-map would either fail outright or copy nonsense across.
				"field_no_map": ["source", "status", "items"],
			}
		},
		target_doc=target_doc,
		postprocess=set_missing_values,
	)


@frappe.whitelist()
def make_order(source_name: str):
	""""Create > Order" on an Inquiry -- same native mapped-doc pattern as
	make_quotation above, for the direct Inquiry -> Sales Order path (no
	Quotation, no retail markup -- order_api.create_order's own direct path,
	same plain approved-dealer-price rate). 12 Nov 2026 rebuild: one Inquiry's
	real-item rows all become lines on the same, single Sales Order draft.

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
	real_rows = [row for row in inquiry.items if row.item]
	if not real_rows:
		frappe.throw(
			_("{0} has no catalog items (only custom style requests) -- it can't become an Order.").format(source_name),
			frappe.ValidationError,
		)

	customer_address, shipping_address_name = default_billing_shipping_for(inquiry.dealer)
	lines = [{"item": row.item, "qty": row.qty} for row in real_rows]

	def set_missing_values(source, target):
		target.customer = source.dealer
		target.transaction_date = frappe.utils.today()
		target.company = default_company()
		target.po_no = source.customer_po
		target.custom_source_type = "Inquiry"
		target.custom_source_ref = source.name
		target.custom_order_channel = auto_classify_channel(source.dealer, lines)
		target.customer_address = customer_address
		target.shipping_address_name = shipping_address_name
		target.items = []
		for row in real_rows:
			price_list_rate = get_price_for_dealer(row.item, source.dealer) or 0
			item_name, stock_uom = frappe.db.get_value("Item", row.item, ["item_name", "stock_uom"])
			target.append(
				"items",
				{
					"item_code": row.item,
					"item_name": item_name,
					"uom": stock_uom,
					"stock_uom": stock_uom,
					"conversion_factor": 1,
					"qty": row.qty,
					"price_list_rate": price_list_rate,
					"rate": price_list_rate,
					"custom_damaged_stock": row.get("damaged_stock"),
					"custom_preferred_batch_no": row.get("preferred_batch_no"),
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
				# is free text) and isn't something this mapper sets anyway. "items"
				# excluded for the same reason as make_quotation's own copy.
				"field_no_map": ["source", "status", "contact_phone", "items"],
			}
		},
		postprocess=set_missing_values,
	)


def _resolve_item_qty(item: str | None, qty: float, uom: str | None, entered_boxes: float | None) -> tuple[float, float | None, str | None, float | None]:
	"""Returns (qty_in_boxes, entered_qty, entered_uom, stored_entered_boxes) for
	one item row -- the same combined-Boxes+Pcs / plain-uom conversion
	_create_inquiry's own per-row loop used to do inline, factored out so it's
	identical for every row instead of copy-pasted."""
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
	return qty, entered_qty, entered_uom, stored_entered_boxes


def _build_inquiry_item_row(
	dealer: str,
	item: str | None,
	qty: float,
	requested_style: str | None = None,
	damaged_stock: bool = False,
	uom: str | None = None,
	entered_boxes: float | None = None,
	preferred_batch_no: str | None = None,
) -> tuple[dict, dict | None]:
	"""Validates and builds ONE Inquiry Item child row dict (not yet inserted --
	_create_inquiry appends every row onto one Inquiry and inserts once). Returns
	(row_dict, duplicate_hint) -- duplicate_hint is BRD C.2.4's own
	find_open_duplicate_inquiries first match for this row, or None.

	BRD C.1.3/C.2.2, client request 2 Nov 2026 -- `uom` (Box/Pcs/Sqft/Sqm/Kg/Gram,
	catalog.utils.ORDER_UOMS) lets a dealer enter qty in whatever unit they think
	in, same as order_api.create_order/quotation_api.create_quotation already do.
	Converted to box-equivalent before anything below (stock comparison, the
	duplicate check, the stored row) ever sees it.

	ISS-2026-00678 -- `damaged_stock` marks this row as the dealer explicitly
	asking for damage/insurance-claim bay stock specifically, not regular
	inventory. ISS-2026-00712 -- `preferred_batch_no` is checked against real
	on-hand stock in that batch, rejected immediately rather than only failing
	later if/when this becomes an order.

	ISS-2026-00636 / MOM 3.6 -- "Custom Style": exactly one of `item`/
	`requested_style`, never both, never neither. Every item-shaped computation
	below (sellability, duplicate check, stock-derived availability) only makes
	sense against a real Item, so the whole item-shaped half is skipped for a
	Custom Style row."""
	if bool(item) == bool(requested_style):
		frappe.throw(_("Exactly one of item or requestedStyle is required per line."), frappe.ValidationError)

	qty, entered_qty, entered_uom, stored_entered_boxes = _resolve_item_qty(item, qty, uom, entered_boxes)

	duplicate_hint = None
	recent_duplicate = None
	if item:
		item_status = frappe.get_cached_value("Item", item, "custom_discontinuation_status") or "Active"
		if not is_sellable(item_status):
			frappe.throw(_("{0} is {1} and can no longer be quoted.").format(item, status_label(item_status)), frappe.ValidationError)

		# BRD C.2.4 — checked before insert, so the new row can't match itself.
		duplicates = find_open_duplicate_inquiries(dealer, item)
		duplicate_hint = duplicates[0]["id"] if duplicates else None

		# Client request, 12 Nov 2026 -- the 24-hour-exact rolling check (sales.utils.
		# recent_inquiry_for), separate from BRD C.2.4's own 7-day one above: this is
		# what sets this row's own duplicate_flag so Pacific staff see it directly,
		# not just as a same-moment hint in this call's own response.
		recent_duplicate = recent_inquiry_for(dealer, item, damaged_stock=bool(damaged_stock))

		if damaged_stock:
			from dms_erp.warehouse.utils import DAMAGE_BAY_TYPES, excluded_stock_for_item

			on_hand = sum(r["boxes"] for r in excluded_stock_for_item(item) if r["bayType"] in DAMAGE_BAY_TYPES)
		else:
			on_hand = total_stock_for_item(item)
		if on_hand <= 0:
			availability_status = "Out of Stock"
		elif on_hand >= qty:
			availability_status = "Available"
		else:
			availability_status = "Partially Available"
	else:
		availability_status = "Open"

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

	row = {
		"item": item,
		"custom_requested_style": requested_style,
		"qty": qty,
		**(
			{"entered_boxes": stored_entered_boxes, "entered_qty": entered_qty, "entered_uom": entered_uom}
			if entered_uom
			else {}
		),
		"damaged_stock": damaged_stock,
		"preferred_batch_no": preferred_batch_no if item else None,
		"availability_status": availability_status,
		"duplicate_flag": 1 if recent_duplicate else 0,
	}
	return row, ({"id": duplicate_hint} if duplicate_hint else None)


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
	damaged_stock: bool = False,
	uom: str | None = None,
	entered_boxes: float | None = None,
	preferred_batch_no: str | None = None,
) -> dict:
	"""Unguarded core of create_inquiry, single-item shape -- also called
	directly by sales.dealer_portal_api.raise_inquiry and most of comms.
	flow_api's own call sites, whose own DMS Dealer session (scoped to its own
	dealer identity, never a caller-supplied one) is the authorization for that
	path, not INQUIRY_WRITE_ROLES. A thin, one-row wrapper over
	_create_inquiry_multi below -- see that function for the real logic; kept
	as its own entry point since every existing single-item caller already has
	exactly these kwargs in hand and shouldn't have to build a one-element list
	itself."""
	return _create_inquiry_multi(
		dealer=dealer,
		items=[
			{
				"item": item,
				"qty": qty,
				"requestedStyle": requested_style,
				"damagedStock": damaged_stock,
				"uom": uom,
				"enteredBoxes": entered_boxes,
				"preferredBatchNo": preferred_batch_no,
			}
		],
		source=source,
		expected_delivery=expected_delivery,
		follow_up_date=follow_up_date,
		assigned_to=assigned_to,
		remarks=remarks,
		contact_name=contact_name,
		contact_phone=contact_phone,
	)


def _create_inquiry_multi(
	dealer: str,
	items: list[dict],
	source: str,
	expected_delivery=None,
	follow_up_date=None,
	assigned_to: str | None = None,
	remarks: str | None = None,
	contact_name: str | None = None,
	contact_phone: str | None = None,
) -> dict:
	"""12 Nov 2026 rebuild -- the real core every Inquiry creation path now goes
	through: one Inquiry, one or more Inquiry Item rows. `items` is a list of
	dicts, each accepting the same keys _build_inquiry_item_row takes (item,
	qty, requestedStyle, damagedStock, uom, enteredBoxes, preferredBatchNo).
	Every row is validated (and, for a real item, priced/stock-checked) before
	any of them are inserted -- a failure on row 3 of 5 must not leave a
	half-built Inquiry behind, same all-or-nothing guarantee dealer_portal_api.
	raise_inquiries' own pre-rebuild version already gave callers.

	Returns the serialized Inquiry plus `duplicatesOf` -- a {lineIndex:
	inquiryId} map for whichever rows (by their position in `items`) matched an
	existing open inquiry, 0-indexed; empty when nothing did."""
	if not items:
		frappe.throw(_("At least one item is required."), frappe.ValidationError)

	if assigned_to is None and set(frappe.get_roles(frappe.session.user)) & INQUIRY_WRITE_ROLES:
		# Only a staff caller defaults to self-assignment -- a dealer-portal session
		# (raise_inquiry(ies)) has no INQUIRY_WRITE_ROLES membership, so its
		# inquiries stay unassigned rather than "assigned to" the dealer's own
		# portal account.
		assigned_to = frappe.session.user

	rows = []
	duplicates_of = {}
	for i, entry in enumerate(items):
		row, duplicate_hint = _build_inquiry_item_row(
			dealer=dealer,
			item=entry.get("item"),
			qty=entry["qty"],
			requested_style=entry.get("requestedStyle"),
			damaged_stock=bool(entry.get("damagedStock")),
			uom=entry.get("uom"),
			entered_boxes=entry.get("enteredBoxes"),
			preferred_batch_no=entry.get("preferredBatchNo"),
		)
		rows.append(row)
		if duplicate_hint:
			duplicates_of[i] = duplicate_hint["id"]

	doc = frappe.get_doc(
		{
			"doctype": "Inquiry",
			"dealer": dealer,
			"items": rows,
			"source": source,
			"status": "Open",
			"expected_delivery": expected_delivery,
			"follow_up_date": follow_up_date,
			"assigned_to": assigned_to,
			"remarks": remarks,
			"contact_name": contact_name,
			"contact_phone": contact_phone,
		}
	)
	doc.insert(ignore_permissions=True)

	opportunity = _mirror_to_opportunity(doc)
	if opportunity:
		doc.db_set("linked_opportunity", opportunity, update_modified=False)

	result = _serialize(doc)
	result["duplicatesOf"] = duplicates_of
	# Back-compat single-item shape for callers that only ever pass one item
	# (every existing one does, via _create_inquiry above) -- same `duplicateOf`
	# key they already read, now derived from row 0 instead of being the whole
	# point of the call.
	result["duplicateOf"] = duplicates_of.get(0)
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

	# 12 Nov 2026 rebuild -- "qty" dropped from here: it used to mean the
	# Inquiry's own single qty, which no longer exists at this level now that an
	# Inquiry can hold several items. Editing one line's qty isn't wired up as a
	# patch-by-key operation (no caller needed it); re-raising the inquiry is the
	# practical path if a quantity was wrong.
	field_map = {
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
	item: str | None = None,
	supplier: str | None = None,
	ordered_qty: float | None = None,
	remarks: str | None = None,
):
	"""12 Nov 2026 rebuild -- a Purchase Order is inherently for one item, but an
	Inquiry can now hold several; `item` picks which row to raise it for
	(required once the Inquiry has more than one real item, same as any
	ambiguous per-row action now needs to say which row). Left unset on a
	single-real-item Inquiry, that one row is used, same as before the
	rebuild."""
	from dms_erp.purchase.po_api import create_purchase_order

	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.status not in PURCHASE_REQUIREMENT_STATUSES:
		frappe.throw(
			_("Inquiry {0} is {1} — only an Open inquiry can become a purchase requirement.").format(
				inquiry, doc.status
			),
			frappe.ValidationError,
		)

	real_rows = [row for row in doc.items if row.item]
	if not real_rows:
		# ISS-2026-00636 -- a Custom Style-only inquiry has only free text, nothing to buy.
		frappe.throw(
			_("Inquiry {0} is a custom style request with no catalog item -- find or create the item first.").format(
				inquiry
			),
			frappe.ValidationError,
		)

	if item:
		row = next((r for r in real_rows if r.item == item), None)
		if not row:
			frappe.throw(_("{0} is not one of this inquiry's items.").format(item), frappe.ValidationError)
	elif len(real_rows) == 1:
		row = real_rows[0]
	else:
		frappe.throw(
			_("Inquiry {0} has {1} items -- say which one (item) to raise a purchase requirement for.").format(
				inquiry, len(real_rows)
			),
			frappe.ValidationError,
		)

	po = create_purchase_order(
		item=row.item,
		ordered_qty=ordered_qty or row.qty,
		supplier=supplier,
		expected_ready_date=expected_ready_date,
		remarks=remarks,
		source_inquiry=inquiry,
	)

	frappe.db.set_value("Inquiry", inquiry, "status", "Mapped to PO")
	return po
