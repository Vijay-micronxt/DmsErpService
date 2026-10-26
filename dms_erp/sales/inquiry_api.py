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

import frappe
from frappe import _

from dms_erp.catalog.utils import (
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
		"assignedTo": doc.assigned_to,
		"remarks": doc.remarks,
		# BRD client req 8.9 -- which of the dealer's own sales staff phoned this in,
		# so several of a dealer's people inquiring about different items at once stay
		# distinguishable. Free text (no dealer-contacts master exists).
		"contactName": doc.contact_name,
		"contactPhone": doc.contact_phone,
		"whatsappReplied": bool(doc.whatsapp_replied),
		"customerPo": doc.customer_po,
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
) -> dict:
	"""Unguarded core of create_inquiry -- also called directly by
	sales.dealer_portal_api.raise_inquiry, whose own DMS Dealer session (scoped to
	its own dealer identity, never a caller-supplied one) is the authorization for
	that path, not INQUIRY_WRITE_ROLES.

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

	doc = frappe.get_doc(
		{
			"doctype": "Inquiry",
			"dealer": dealer,
			"item": item,
			"custom_requested_style": requested_style,
			"request_group": request_group,
			"qty": qty,
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
	}

	doc = frappe.get_doc("Inquiry", inquiry)
	for key, value in patch.items():
		fieldname = field_map.get(key)
		if fieldname:
			doc.set(fieldname, value)
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
