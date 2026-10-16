"""BRD C.13 — the dealer-facing self-service portal's API surface. Every endpoint here
is a whitelisted method under dms_erp.sales.dealer_portal_api., which is exactly the
prefix auth.middleware._enforce_dealer_scope confines a DMS Dealer-only session to —
that middleware guard, not a per-function role check, is what keeps a dealer portal
session out of the rest of this app.

"dealer" == ERPNext Customer everywhere in this codebase (see sales/dealer_api.py,
catalog/dealer_catalog_api.py, pricing/dealer_classification.py) — there is no
separate Dealer doctype. A dealer-portal session never supplies its own dealer id as
a request parameter; _current_dealer() resolves it server-side from
frappe.session.user -> User.custom_dealer -> Customer, which is the whole point of
scoping it this way (a dealer can't ask for another dealer's data by passing a
different id).

Catalog, item detail, convert-to-order and raise-inquiry all delegate to the same
core functions the staff app uses (catalog.api.get_product, sales.inquiry_api.
_create_inquiry, sales.order_api._create_order) rather than reimplementing them —
this dealer session's own identity/scoping is the authorization for those calls,
in place of the staff role checks those modules' whitelisted wrappers use.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

from dms_erp.catalog.api import get_product, resolve_dealer_code
from dms_erp.catalog.dealer_catalog_api import browsable_catalog_for, catalog_for, is_visible
from dms_erp.catalog.utils import is_sellable, item_weight_per_box_kg
from dms_erp.pagination import clamp
from dms_erp.sales.dealer_api import _assert_email_available
from dms_erp.sales.invoice_api import _has as _invoice_has
from dms_erp.sales.invoice_api import _invoice_detail
from dms_erp.sales.invoice_api import _serialize_row as _serialize_invoice_row
from dms_erp.sales.inquiry_api import _create_inquiry, _inquiry_filters, _serialize as _serialize_inquiry
from dms_erp.sales.order_api import _create_order
from dms_erp.sales.order_api import _serialize as _serialize_order
from dms_erp.sales.quotation_api import _convert_to_order
from dms_erp.warehouse.utils import all_batches, top_batches, total_stock_for_item


MAX_ENQUIRY_ITEMS = 50


def _current_dealer() -> str:
	dealer = frappe.db.get_value("User", frappe.session.user, "custom_dealer")
	if not dealer:
		frappe.throw(_("This account is not linked to a dealer."), frappe.PermissionError)
	return dealer


def _catalog_entry(item_code: str, dealer: str, every_batch: bool = False) -> dict:
	"""A trimmed, dealer-safe view of catalog.api.get_product's full serialization --
	drops dealerCodes (every dealer's own item code for this item, including other
	dealers') down to just this dealer's own code, and adds the top-3 on-hand
	batches BRD C.13.1 wants shown at item-detail time.

	No price is ever included -- not `price`, and not the `dealerPrice` that
	get_product carries for staff either. Pacific's price to a dealer depends on
	delivery and other details that only exist once a quotation is prepared, so a
	catalog price shown earlier would just be a number staff then have to walk back
	(client request, Oct 2026: "don't show price before quotation"). The dealer's
	price is the quotation's (see get_my_quotation). This replaces BRD C.13.1's
	per-dealer "price (if enabled)" switch for the portal; Customer.
	custom_price_visible is no longer read anywhere. Staff-facing screens are
	unaffected, since they never call through here.

	`orderable` (ISS-2026-00644) is whether this item is actually in this dealer's
	Dealer Catalog assignment (dealer_catalog_api.is_visible) -- get_catalog now shows
	every sellable item, assigned or not, so the frontend needs this flag to tell an
	orderable item apart from one it should only let the dealer inquire on."""
	product = get_product(item_code)
	own_code = next((row["customerItemCode"] for row in product["dealerCodes"] if row["dealer"] == dealer), None)
	del product["dealerCodes"]
	product.pop("dealerPrice", None)
	product["dealerCode"] = own_code
	# The catalog list keeps the cheap top-3 preview; an item's own detail page lists
	# every batch (client request, Oct 2026: which batch the stock belongs to).
	product["topBatches"] = all_batches(item_code) if every_batch else top_batches(item_code)
	product["orderable"] = is_visible(dealer, item_code)
	return product


_CATALOG_SEARCH_FIELDS = (
	"item_name",
	"custom_colour_attribute_1",
	"custom_colour_attribute_2",
	"custom_design_attribute_1",
	"custom_design_attribute_2",
)


def _catalog_search_matches(candidate_codes: set[str], search: str) -> set[str]:
	"""get_catalog's one search box now matches item name OR any of the four
	colour/design attributes -- the dealer portal used to expose those as four
	separate dropdown filters (ISS-2026-00632 #4), but typing "Rustic" into the
	one search box dealers already use is a more natural fit than hunting through
	four dropdowns for it, so the dropdowns were dropped in favour of this.
	frappe.db.count doesn't accept or_filters (only get_all does), so the OR is
	resolved into an explicit name-list first, same union-of-get_all pattern as
	warehouse.inward_api.list_trucks/sales.order_api.list_orders."""
	if not candidate_codes:
		return set()
	matches: set[str] = set()
	for field in _CATALOG_SEARCH_FIELDS:
		matches |= set(
			frappe.get_all(
				"Item",
				filters={"name": ["in", list(candidate_codes)], field: ["like", f"%{search}%"]},
				pluck="name",
			)
		)
	return matches


@frappe.whitelist(methods=["GET"])
def get_catalog(
	search: str | None = None,
	category: str | None = None,
	color: str | None = None,
	colour_attribute_1: str | None = None,
	colour_attribute_2: str | None = None,
	design_attribute_1: str | None = None,
	design_attribute_2: str | None = None,
	scope: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	"""`scope` splits the one browse list in two for the portal's separate sections:
	"assigned" is only this dealer's own Dealer Catalog (what they can order),
	"other" is every sellable item outside it (enquire only); left out, both
	together, as before.

	BRD C.1.5/C.13.1's dealer catalog, browse-widened by ISS-2026-00644: every
	currently-sellable item in the system, not just this dealer's own Dealer Catalog
	assignment -- an unassigned item still shows up (with `orderable: false` on its
	entry, see _catalog_entry), it just can't be ordered through convert_to_order.
	`search` matches item name OR any of the four colour/design attributes (see
	_catalog_search_matches) -- the dealer portal's own standalone attribute
	dropdowns were dropped in favour of this one box covering both; resolve_code is
	the separate lookup for a dealer's own code or the company item code.
	colour_attribute_1/2/design_attribute_1/2 stay as exact-match filter params
	(and their list_catalog_* option endpoints) for any future caller that wants a
	precise filter rather than free-text search, even though the dealer portal's
	own UI no longer sends them."""
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)

	item_codes = set(browsable_catalog_for(dealer))
	if scope in ("assigned", "other"):
		assigned = set(catalog_for(dealer))
		item_codes = item_codes & assigned if scope == "assigned" else item_codes - assigned
	filters = {"name": ["in", list(item_codes) or [""]]}
	if category:
		filters["item_group"] = category
	if color:
		filters["custom_color"] = color
	if colour_attribute_1:
		filters["custom_colour_attribute_1"] = colour_attribute_1
	if colour_attribute_2:
		filters["custom_colour_attribute_2"] = colour_attribute_2
	if design_attribute_1:
		filters["custom_design_attribute_1"] = design_attribute_1
	if design_attribute_2:
		filters["custom_design_attribute_2"] = design_attribute_2

	if search:
		pre_filtered = set(frappe.get_all("Item", filters=filters, pluck="name"))
		filters["name"] = ["in", list(_catalog_search_matches(pre_filtered, search)) or [""]]

	total = frappe.db.count("Item", filters=filters)
	codes = frappe.get_all(
		"Item", filters=filters, pluck="name", order_by="item_code asc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_catalog_entry(code, dealer) for code in codes],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


def _distinct_catalog_values(dealer: str, fieldname: str) -> list[str]:
	"""Scoped to this dealer's own browsable catalog (ISS-2026-00644: every sellable
	item, assigned or not -- see get_catalog), not every Item on the site -- a
	colour/design filter option with no matching item anywhere wouldn't just be an
	empty result, it'd be a filter that can never match anything at all."""
	item_codes = list(browsable_catalog_for(dealer))
	if not item_codes:
		return []
	values = frappe.get_all(
		"Item", filters={"name": ["in", item_codes], fieldname: ["is", "set"]}, pluck=fieldname, distinct=True
	)
	return sorted(set(values))


@frappe.whitelist(methods=["GET"])
def list_catalog_colors() -> list[str]:
	return _distinct_catalog_values(_current_dealer(), "custom_color")


@frappe.whitelist(methods=["GET"])
def list_catalog_colour_attribute_1() -> list[str]:
	return _distinct_catalog_values(_current_dealer(), "custom_colour_attribute_1")


@frappe.whitelist(methods=["GET"])
def list_catalog_colour_attribute_2() -> list[str]:
	return _distinct_catalog_values(_current_dealer(), "custom_colour_attribute_2")


@frappe.whitelist(methods=["GET"])
def list_catalog_design_attribute_1() -> list[str]:
	return _distinct_catalog_values(_current_dealer(), "custom_design_attribute_1")


@frappe.whitelist(methods=["GET"])
def list_catalog_design_attribute_2() -> list[str]:
	return _distinct_catalog_values(_current_dealer(), "custom_design_attribute_2")


@frappe.whitelist(methods=["GET"])
def resolve_code(code: str) -> dict | None:
	"""BRD C.13.1 — search by the dealer's own code OR the company item code, either
	one. resolve_dealer_code only ever resolved the dealer's own mapped code; this
	adds the company-code fallback (must still be sellable, ISS-2026-00644: browsable
	regardless of this dealer's own Dealer Catalog assignment, same as get_catalog)
	so both entry points actually work from the portal, not just one."""
	dealer = _current_dealer()

	product = resolve_dealer_code(dealer, code)
	if not product:
		if frappe.db.exists("Item", code):
			status = frappe.get_cached_value("Item", code, "custom_discontinuation_status") or "Active"
			if is_sellable(status):
				product = get_product(code)

	if not product:
		return None
	return _catalog_entry(product["id"], dealer, every_batch=True)


@frappe.whitelist(methods=["GET"])
def get_item(item: str) -> dict:
	"""ISS-2026-00644 — gated on existence+sellability, not this dealer's own Dealer
	Catalog assignment: a dealer can view the detail of an item Purchase hasn't
	assigned to them yet (orderable: false on the response is what actually stops
	them booking it — see _catalog_entry/convert_to_order)."""
	dealer = _current_dealer()
	if item not in set(browsable_catalog_for(dealer)):
		frappe.throw(_("{0} is not a valid item.").format(item), frappe.PermissionError)
	return _catalog_entry(item, dealer, every_batch=True)


@frappe.whitelist(methods=["POST"])
def raise_inquiry(item: str, qty: float, remarks: str | None = None):
	"""BRD C.13.1 — the out-of-stock / "raise an enquiry" path. Always source="Web"
	and always unassigned (see inquiry_api._create_inquiry's own staff-only
	self-assignment default) since this is the dealer, not staff, raising it."""
	dealer = _current_dealer()
	data = _create_inquiry(dealer, item, qty, source="Web", remarks=remarks)
	data["orderable"] = is_visible(dealer, item)
	data.pop("price", None)
	return data


@frappe.whitelist(methods=["POST"])
def raise_inquiries(items, remarks: str | None = None) -> dict:
	"""Several items enquired on in one go (the enquiry list on the portal). Each
	item becomes its own Inquiry, exactly as raise_inquiry makes one -- the Inquiry
	doctype is one item per record, and everything downstream (stock status,
	missed-demand reporting, quoting) is built on that -- but they all carry the
	same `requestGroup` (the first one's number), so staff see they came together
	and quote them as a single quotation (create_quotation already merges several
	inquiries of one dealer). `items` is a list of {item, qty}, JSON when sent as a
	form field. Every line is validated before anything is created, and a failure
	partway rolls the whole request back (Frappe's per-request transaction), so a
	dealer never ends up with half a list raised."""
	dealer = _current_dealer()
	entries = frappe.parse_json(items) if isinstance(items, str) else items
	if not entries:
		frappe.throw(_("Add at least one item."), frappe.ValidationError)
	if len(entries) > MAX_ENQUIRY_ITEMS:
		frappe.throw(_("An enquiry can list at most {0} items.").format(MAX_ENQUIRY_ITEMS), frappe.ValidationError)

	allowed = set(browsable_catalog_for(dealer))
	seen: set[str] = set()
	for entry in entries:
		item = entry.get("item")
		if item not in allowed:
			frappe.throw(_("{0} is not a valid item.").format(item), frappe.PermissionError)
		if item in seen:
			frappe.throw(_("{0} is listed twice -- combine the quantities.").format(item), frappe.ValidationError)
		seen.add(item)
		if flt(entry.get("qty")) <= 0:
			frappe.throw(_("Enter a quantity above zero for {0}.").format(item), frappe.ValidationError)

	created: list[dict] = []
	group = None
	for entry in entries:
		inquiry = _create_inquiry(
			dealer, entry["item"], flt(entry["qty"]), source="Web", remarks=remarks, request_group=group
		)
		if group is None:
			group = inquiry["id"]
			frappe.db.set_value("Inquiry", group, "request_group", group, update_modified=False)
			inquiry["requestGroup"] = group
		inquiry["orderable"] = is_visible(dealer, entry["item"])
		inquiry.pop("price", None)
		created.append(inquiry)
	return {"requestGroup": group, "items": created}


@frappe.whitelist(methods=["GET"])
def suggest_alternatives(item: str, limit: int = 5) -> list[dict]:
	"""BRD C.13.1 — "see alternative/recommended items" when the item is out of stock.

	First the alternative STAFF chose for this item (Products -> Edit -> Alternative item;
	flagged `isRecommended`) -- but only when it's in this dealer's own catalog and has
	stock, the same two checks WhatsApp's out-of-stock reply makes: recommending an item
	the dealer can't order, or another dead end, isn't an alternative. Then same-series
	items in the dealer's catalog, ranked by on-hand stock, excluding the item itself --
	the closest approximation to "recommended" available without a recommendation
	engine (none exists anywhere in this app)."""
	from dms_erp.catalog.api import _get_alt_item

	dealer = _current_dealer()
	limit, _offset = clamp(limit, 0)

	assigned = set(catalog_for(dealer))
	chosen = _get_alt_item(item)
	picks = [chosen] if chosen and chosen != item and chosen in assigned and total_stock_for_item(chosen) > 0 else []

	series_ref = frappe.get_cached_value("Item", item, "custom_series_ref")
	item_codes = assigned - {item} - set(picks)
	if series_ref:
		same_series = set(frappe.get_all("Item", filters={"custom_series_ref": series_ref}, pluck="name"))
		candidates = item_codes & same_series
	else:
		candidates = item_codes

	ranked = sorted(candidates, key=lambda code: total_stock_for_item(code), reverse=True)
	entries = [_catalog_entry(code, dealer) for code in (picks + ranked)[:limit]]
	for entry in entries:
		entry["isRecommended"] = entry["id"] in picks
	return entries


@frappe.whitelist(methods=["POST"])
def convert_to_order(inquiry: str, expected_dispatch, customer_po: str):
	"""No longer offered by the portal: an enquiry now becomes an order only through
	a quotation the dealer accepts (accept_quotation below), per the client's "no
	quotation is being sent to dealers before order confirmation" issue. Kept
	callable (and tested) rather than deleted -- it's still the one path that
	builds an order straight from an inquiry's own lines.

	BRD C.13.1 — "enter their own PO number to tag/close it." Only converts an
	inquiry this dealer actually owns, at the same in-stock/priced items it was
	raised for -- lines come from the inquiry itself, not caller-supplied, so a
	dealer session can't order arbitrary items through this endpoint.

	Deliberately NOT gated for credit-limit exceedance (BRD C.11 trigger #1) the
	way order_api.create_order/quotation_api.convert_to_order and
	comms.flow_api's WhatsApp order-placement path now are: this endpoint's
	response is consumed by the separate dealer-portal frontend, and returning
	`{"approvalRequired": True, ...}` here instead of an Order would be a
	response-shape change that frontend was never updated to handle. What IS
	fixed here: _create_order can still raise ERPNext's own native
	credit-limit-exceeded validation (an HTML-formatted message naming internal
	Credit Controller users and their emails, never meant for a dealer to see;
	see sales.utils.safe_customer_message's own docstring for the real report
	that surfaced this) -- that text must never reach the dealer verbatim.

	ISS-2026-00644: raise_inquiry (and get_catalog/get_item) now let a dealer
	inquire on/view any sellable item, assigned or not, but ordering stays gated to
	this dealer's actual Dealer Catalog assignment (is_visible) -- the inquiry that
	got them here can exist for an unassigned item, but converting it to a real
	order can't."""
	dealer = _current_dealer()
	if not customer_po:
		frappe.throw(_("customer_po is required."), frappe.ValidationError)

	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.dealer != dealer:
		frappe.throw(_("Inquiry {0} does not belong to you.").format(inquiry), frappe.PermissionError)
	if doc.status in {"Converted to Order", "Rejected", "Closed"}:
		frappe.throw(_("Inquiry {0} is {1} and can no longer be converted.").format(inquiry, doc.status), frappe.ValidationError)
	if not doc.item:
		# ISS-2026-00636 -- a Custom Style request (doc.custom_requested_style set,
		# no real Item): there's nothing to build a Sales Order line from, so this
		# is rejected with its own clear message rather than falling into
		# is_visible(dealer, None), which would also reject it, just less clearly.
		from dms_erp.sales.utils import dealer_contact_line

		frappe.throw(
			_("This is a custom-style enquiry with no catalog item yet -- it can't be converted to an order. {0}").format(
				dealer_contact_line(dealer)
			),
			frappe.ValidationError,
		)
	if not is_visible(dealer, doc.item):
		from dms_erp.sales.utils import dealer_contact_line

		frappe.throw(
			_("{0} isn't yet set up for ordering on your account. {1}").format(doc.item, dealer_contact_line(dealer)),
			frappe.PermissionError,
		)

	lines = [{"item": doc.item, "qty": doc.qty}]
	try:
		return _run_as_system(lambda: _create_order(dealer, lines, expected_dispatch, inquiry, customer_po=customer_po))
	except (frappe.PermissionError, frappe.ValidationError) as e:
		from dms_erp.sales.utils import safe_customer_message

		frappe.throw(safe_customer_message(e, dealer), type(e))


@frappe.whitelist(methods=["GET"])
def list_my_inquiries(status: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0):
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)
	filters = _inquiry_filters(dealer, status, search)
	total = frappe.db.count("Inquiry", filters=filters)
	names = frappe.get_all(
		"Inquiry", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize_dealer_inquiry(frappe.get_doc("Inquiry", name), dealer) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


def _enquiry_status(statuses: list[str]) -> str:
	"""One status for a whole enquiry. When every item is in the same state it's that state;
	when they differ (two quoted, one out of stock) there is no honest single word, so it
	says "Mixed" and the detail page shows each item's own status."""
	distinct = set(statuses)
	return statuses[0] if len(distinct) == 1 else "Mixed"


def _serialize_enquiry(key: str, docs: list, dealer: str) -> dict:
	"""One enquiry as the dealer sees it: the items they asked about together. The Inquiry
	doctype is one item per record (stock status, reorder demand and quoting all hang off
	that), so an enquiry raised with several items is several records sharing a
	request_group -- this gathers them back into the single enquiry the dealer made.
	A lone inquiry is just an enquiry of one item, keyed by its own number."""
	items = [_serialize_dealer_inquiry(doc, dealer) for doc in sorted(docs, key=lambda d: (d.creation, d.name))]
	return {
		"id": key,
		"number": key,
		"date": items[0]["date"],
		"createdAt": items[0]["createdAt"],
		"status": _enquiry_status([i["status"] for i in items]),
		"itemCount": len(items),
		"remarks": next((i["remarks"] for i in items if i["remarks"]), None),
		"linkedQuotations": sorted({i["linkedQuotation"] for i in items if i["linkedQuotation"]}),
		"items": items,
	}


@frappe.whitelist(methods=["GET"])
def list_my_enquiries(status: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0):
	"""The dealer's enquiries, one entry per enquiry they made -- items raised together from
	the enquiry list come back as ONE entry holding all of them (see _serialize_enquiry).
	Paginated by enquiry, not by item, so a 3-item enquiry is never split across pages.
	`status`/`search` pick the enquiries that have at least one matching item; the whole
	enquiry is then returned, not only the matching items."""
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)
	rows = frappe.get_all(
		"Inquiry",
		filters=_inquiry_filters(dealer, status, search),
		fields=["name", "request_group", "creation"],
		order_by="creation desc",
	)
	keys: list[str] = []
	for row in rows:
		key = row.request_group or row.name
		if key not in keys:
			keys.append(key)
	page = keys[offset : offset + limit]
	return {
		"items": [_enquiry_for(key, dealer) for key in page],
		"total": len(keys),
		"limit": limit,
		"offset": offset,
	}


def _enquiry_for(key: str, dealer: str) -> dict:
	names = frappe.get_all("Inquiry", filters={"dealer": dealer, "request_group": key}, pluck="name")
	if not names and frappe.db.exists("Inquiry", {"name": key, "dealer": dealer}):
		names = [key]
	if not names:
		frappe.throw(_("Enquiry {0} does not belong to you.").format(key), frappe.PermissionError)
	return _serialize_enquiry(key, [frappe.get_doc("Inquiry", name) for name in names], dealer)


@frappe.whitelist(methods=["GET"])
def get_my_enquiry(enquiry: str) -> dict:
	"""One enquiry with all its items. `enquiry` is the id list_my_enquiries returned: the
	shared group id for items raised together, or the inquiry's own number for a single one."""
	return _enquiry_for(enquiry, _current_dealer())


@frappe.whitelist(methods=["GET"])
def get_my_inquiry(inquiry: str) -> dict:
	dealer = _current_dealer()
	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.dealer != dealer:
		frappe.throw(_("Inquiry {0} does not belong to you.").format(inquiry), frappe.PermissionError)
	return _serialize_dealer_inquiry(doc, dealer)


def _serialize_dealer_inquiry(doc, dealer: str) -> dict:
	"""ISS-2026-00644 -- a dealer can now enquire on items outside their assigned
	catalog, and on a Custom Style with no item at all (ISS-2026-00636), but
	convert_to_order still refuses both. `orderable` is that same rule computed
	up front, so the portal can hide "Convert to order" instead of showing a
	button that only ever fails. convert_to_order stays the real gate -- this is
	a display hint, re-checked there."""
	data = _serialize_inquiry(doc)
	data["orderable"] = bool(doc.item) and is_visible(dealer, doc.item)
	data["productName"] = frappe.get_cached_value("Item", doc.item, "item_name") if doc.item else None
	# The tier price inquiry_api's serializer adds for staff -- a price before a
	# quotation, which the dealer portal never shows (see _catalog_entry).
	data.pop("price", None)
	return data


@frappe.whitelist(methods=["GET"])
def list_my_orders(stage: str | None = None, limit: int = 20, offset: int = 0):
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)
	filters = {"customer": dealer}
	if stage:
		filters["custom_fulfillment_stage"] = stage
	total = frappe.db.count("Sales Order", filters=filters)
	names = frappe.get_all(
		"Sales Order", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize_order(frappe.get_doc("Sales Order", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_my_order(order: str) -> dict:
	dealer = _current_dealer()
	doc = frappe.get_doc("Sales Order", order)
	if doc.customer != dealer:
		frappe.throw(_("Order {0} does not belong to you.").format(order), frappe.PermissionError)
	return _serialize_order(doc)


# MOM 07-Oct-2026 §3.4/#11, refined by ISS-2026-00634: "Dealers can cancel an order
# only within the first 24 hours of Sales Order creation. After 24 hours,
# cancellation request must go through internal approval." -- within the window a
# dealer cancels immediately, no approval gate; past it, the request is queued
# through the same approvals.gate_authorized_action/"Amend Or Cancel Submitted
# Document" trigger order_api.advance_order_stage's staff-initiated cancel already
# uses, rather than being refused outright -- the dealer's own role set never
# clears DECIDE_ROLES, so this always queues a genuine Pending request for
# Management, never auto-approves.
DEALER_CANCEL_WINDOW_HOURS = 24


QUOTATION_ACCEPTABLE_STATUSES = {"Open", "Replied"}


def _serialize_dealer_quotation(doc) -> dict:
	"""The dealer's view of a Quotation. Deliberately not quotation_api._serialize:
	that one is the staff shape and carries Pacific's own markup %, the sales
	channel and, per line, the item's purchase cost and below-cost flag -- all
	internal, none of it a dealer's business. What's here is what the offer
	actually says: items, rates, discounts, tax, freight, validity.

	Freight is a separate line, not folded into grand_total: it's stored on the
	Quotation as its own field (custom_freight), ERPNext's tax engine never sees
	it, so `totalPayable` adds it explicitly -- the same "goods + freight" total
	the staff Quotation Builder shows."""
	expired = bool(doc.valid_till) and getdate(doc.valid_till) < getdate(today())
	can_accept = doc.docstatus == 1 and doc.status in QUOTATION_ACCEPTABLE_STATUSES and not expired
	freight = flt(doc.custom_freight)
	lines = []
	for row in doc.items:
		weight = item_weight_per_box_kg(row.item_code)
		lines.append(
			{
				"itemCode": row.item_code,
				"itemName": row.item_name,
				"qty": row.qty,
				"priceListRate": row.price_list_rate,
				"discountPercentage": row.discount_percentage,
				"rate": row.rate,
				"amount": row.amount,
				"deliveryDate": row.get("custom_delivery_date"),
				"weightPerBoxKg": weight,
				"totalWeightKg": weight * row.qty if weight is not None else None,
			}
		)
	return {
		"id": doc.name,
		"number": doc.name,
		"date": doc.transaction_date,
		"validTill": doc.valid_till,
		"status": "Expired" if expired and doc.status in QUOTATION_ACCEPTABLE_STATUSES else doc.status,
		"canAccept": can_accept,
		"lines": lines,
		"netTotal": doc.net_total,
		"taxes": [{"description": row.description, "rate": row.rate, "amount": row.tax_amount} for row in doc.taxes],
		"totalTaxesAndCharges": doc.total_taxes_and_charges,
		"freight": freight,
		"deliveryPincode": doc.get("custom_delivery_pincode"),
		"totalPayable": flt(doc.grand_total) + freight,
		"inquiryIds": frappe.get_all("Inquiry", filters={"linked_quotation": doc.name}, pluck="name"),
	}


def _run_as_system(fn):
	"""Runs `fn` as Administrator, then restores the dealer session. Building a Sales
	Order calls into ERPNext internals (get_item_details and others) that check the
	*session user's* read permission on Item, Customer and so on, and the DMS Dealer
	role has no DocPerm on any ERPNext doctype -- on purpose: a dealer session is
	confined to this module's endpoints by auth.middleware, never to ERPNext's own
	desk/API. Granting that role broad read access just to make an order creatable
	would be the wrong way round, so the one trusted call runs as the system instead,
	after this module's own ownership/expiry/sellability checks. (The only visible
	side effect: the order's owner reads Administrator, not the dealer's portal
	account; the order is the dealer's by customer, which is what every dealer-facing
	query filters on.)"""
	dealer_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return fn()
	finally:
		frappe.set_user(dealer_user)


def _own_quotation(quotation: str, dealer: str):
	doc = frappe.get_doc("Quotation", quotation)
	if doc.party_name != dealer or doc.docstatus != 1:
		frappe.throw(_("Quotation {0} does not belong to you.").format(quotation), frappe.PermissionError)
	return doc


@frappe.whitelist(methods=["GET"])
def list_my_quotations(status: str | None = None, limit: int = 20, offset: int = 0):
	"""Quotations Pacific has prepared for this dealer. A quotation exists for the
	dealer the moment staff create it (the Quotation Builder submits it
	immediately), so there is no separate "send" step to wait for. Cancelled
	versions left behind by an amendment are history, not offers -- excluded."""
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)
	filters = {"quotation_to": "Customer", "party_name": dealer, "docstatus": 1}
	if status:
		filters["status"] = status
	total = frappe.db.count("Quotation", filters=filters)
	names = frappe.get_all(
		"Quotation", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize_dealer_quotation(frappe.get_doc("Quotation", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_my_quotation(quotation: str) -> dict:
	return _serialize_dealer_quotation(_own_quotation(quotation, _current_dealer()))


@frappe.whitelist(methods=["POST"])
def accept_quotation(quotation: str, expected_dispatch, customer_po: str):
	"""The dealer's order confirmation. An enquiry becomes an order only here, on a
	quotation Pacific has priced -- this replaces the portal's old straight
	"convert enquiry to order" (see convert_to_order). The order is built from the
	quotation itself (every line, rate, discount and tax exactly as quoted), never
	from caller-supplied lines, with the dealer's own PO number on it, and every
	inquiry the quotation covered is closed out as Converted to Order.

	Same stance on credit limit as convert_to_order and for the same reason: not
	queued for Management approval here (see sales.credit_limit's docstring), but
	ERPNext's own native credit-limit error is run through safe_customer_message so
	its internal HTML/emails never reach a dealer."""
	from dms_erp.sales.utils import dealer_contact_line, safe_customer_message

	dealer = _current_dealer()
	if not (customer_po or "").strip():
		frappe.throw(_("customer_po is required."), frappe.ValidationError)

	doc = _own_quotation(quotation, dealer)
	if doc.status not in QUOTATION_ACCEPTABLE_STATUSES:
		frappe.throw(
			_("Quotation {0} is {1} and can't be accepted. {2}").format(quotation, doc.status, dealer_contact_line(dealer)),
			frappe.ValidationError,
		)
	if doc.valid_till and getdate(doc.valid_till) < getdate(today()):
		frappe.throw(
			_("Quotation {0} expired on {1}. Please ask for a fresh quotation. {2}").format(
				quotation, doc.valid_till, dealer_contact_line(dealer)
			),
			frappe.ValidationError,
		)
	for row in doc.items:
		status = frappe.get_cached_value("Item", row.item_code, "custom_discontinuation_status") or "Active"
		if not is_sellable(status):
			frappe.throw(
				_("{0} is no longer available, so this quotation can't be accepted. {1}").format(
					row.item_code, dealer_contact_line(dealer)
				),
				frappe.ValidationError,
			)

	try:
		return _run_as_system(
			lambda: _convert_to_order(
				quotation, expected_dispatch=expected_dispatch, customer_po=customer_po.strip(), ignore_permissions=True
			)
		)
	except (frappe.PermissionError, frappe.ValidationError) as e:
		frappe.throw(safe_customer_message(e, dealer), type(e))


def _own_invoice_filters(dealer: str) -> dict:
	"""Submitted invoices and credit notes only -- not the Tally migration's
	opening-balance entries (an accounting carry-forward, not something this
	dealer was ever invoiced) or proformas (not a tax invoice)."""
	filters = {"customer": dealer, "docstatus": 1}
	if _invoice_has("is_opening"):
		filters["is_opening"] = ["!=", "Yes"]
	if _invoice_has("tally_voucher_type"):
		filters["tally_voucher_type"] = ["not like", "%proforma%"]
	return filters


@frappe.whitelist(methods=["GET"])
def list_my_invoices(search: str | None = None, limit: int = 20, offset: int = 0):
	"""This dealer's own Sales Invoices -- everything submitted against them,
	including the Tally-migrated history, not only invoices raised from an app
	order. `search` matches the invoice number, Tally voucher number, vehicle or LR
	number."""
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)
	filters = _own_invoice_filters(dealer)
	if search:
		pattern = ["like", f"%{search}%"]
		matches: set[str] = set()
		for field in ("name", "tally_voucher_no", "tally_vehicle_no", "tally_lr_no"):
			if _invoice_has(field):
				matches |= set(frappe.get_all("Sales Invoice", filters={**filters, field: pattern}, pluck="name"))
		filters["name"] = ["in", list(matches) or [""]]
	total = frappe.db.count("Sales Invoice", filters=filters)
	names = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		pluck="name",
		order_by="posting_date desc, creation desc",
		limit_start=offset,
		limit_page_length=limit,
	)
	return {
		"items": [_serialize_invoice_row(frappe.get_doc("Sales Invoice", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_my_invoice(invoice: str) -> dict:
	dealer = _current_dealer()
	doc = frappe.get_doc("Sales Invoice", invoice)
	if doc.customer != dealer or doc.docstatus != 1:
		frappe.throw(_("Invoice {0} does not belong to you.").format(invoice), frappe.PermissionError)
	return _invoice_detail(doc)


@frappe.whitelist(methods=["POST"])
def cancel_my_order(order: str, note: str | None = None) -> dict:
	from frappe.utils import now_datetime, time_diff_in_hours

	from dms_erp.approvals.api import gate_authorized_action
	from dms_erp.sales.order_api import _cancel_order_action

	dealer = _current_dealer()
	doc = frappe.get_doc("Sales Order", order)
	if doc.customer != dealer:
		frappe.throw(_("Order {0} does not belong to you.").format(order), frappe.PermissionError)

	stage = doc.custom_fulfillment_stage
	if stage in ("Delivered", "Cancelled"):
		frappe.throw(_("Order {0} can no longer be cancelled (currently {1}).").format(order, stage), frappe.ValidationError)

	age_hours = time_diff_in_hours(now_datetime(), doc.creation)
	if age_hours <= DEALER_CANCEL_WINDOW_HOURS:
		doc.custom_fulfillment_stage = "Cancelled"
		doc.append(
			"custom_stage_history",
			{"stage": "Cancelled", "at": now_datetime(), "by": frappe.session.user, "note": note or "Cancelled by dealer"},
		)
		doc.save(ignore_permissions=True)
		return {"cancelled": True, "approvalRequired": False, "order": _serialize_order(doc)}

	result = gate_authorized_action(
		trigger_type="Amend Or Cancel Submitted Document",
		reference_doctype="Sales Order",
		reference_name=order,
		reason=(
			f"Dealer {dealer} is requesting to cancel {order}, placed more than "
			f"{DEALER_CANCEL_WINDOW_HOURS} hours ago (outside the dealer self-cancel window)."
		),
		action_fn=_cancel_order_action,
		action_kwargs=dict(order=order, note=note or "Cancellation requested by dealer, past the 24-hour window"),
		applier_action="cancel",
	)
	# A dealer-portal session never clears DECIDE_ROLES, so gate_authorized_action
	# always takes the "queue a Pending request" branch here, never the
	# auto-approve-and-run-now branch -- this assertion documents that invariant
	# rather than silently trusting it.
	if "approvalRequired" not in result:
		frappe.throw(_("Unexpected: a dealer session auto-approved its own cancellation request."))
	return {"cancelled": False, "approvalRequired": True, "approval": result["approval"]}


def _outstanding_for(dealer: str) -> float:
	"""Unguarded core of my_dues -- also called directly by comms.flow_api's
	get_outstanding_due, whose caller is resolved from a WhatsApp phone number
	rather than a dealer-portal session, so it has no frappe.session.user to run
	_current_dealer() against. Read from the ledger, not Sales Invoice.outstanding_amount
	-- see finance.ledger for why the per-invoice figure overstates dues."""
	from dms_erp.finance.ledger import party_balances
	from dms_erp.warehouse.utils import default_company

	return party_balances("Customer", [dealer], default_company()).get(dealer, 0.0)


@frappe.whitelist(methods=["GET"])
def my_dues() -> dict:
	"""BRD C.13.1 — outstanding dues."""
	return {"outstanding": _outstanding_for(_current_dealer())}


@frappe.whitelist(methods=["GET"])
def my_profile() -> dict:
	dealer = _current_dealer()
	doc = frappe.get_doc("Customer", dealer)
	return {
		"id": doc.name,
		"name": doc.customer_name,
		"phone": doc.custom_phone,
		"email": doc.custom_email,
		"classification": doc.custom_dealer_classification,
	}


@frappe.whitelist(methods=["POST", "PUT"])
def update_my_email(email: str):
	"""BRD C.13 — a dealer sets their own email here (not phone, which stays
	OTP-delivery-only and whatever staff set it to at onboarding). This is the one
	prerequisite auth.dealer_api.set_my_password checks for before letting a dealer
	set a portal password: email + password is a second way in, so the email that
	login resolves by has to come from somewhere, and self-service here (rather
	than only ever staff-set, like phone) is what makes that actually reachable
	without staff having to do it for every dealer first."""
	dealer = _current_dealer()
	email = (email or "").strip()
	if not email:
		frappe.throw(_("An email is required."), frappe.ValidationError)

	_assert_email_available(email, exclude=dealer)

	doc = frappe.get_doc("Customer", dealer)
	doc.custom_email = email
	doc.save(ignore_permissions=True)
	return my_profile()
