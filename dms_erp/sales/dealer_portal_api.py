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

import math

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

from dms_erp.catalog.api import _serialize as _serialize_item
from dms_erp.catalog.api import get_product, resolve_dealer_code
from dms_erp.catalog.dealer_catalog_api import _dealer_catalog_export_html, browsable_catalog_for, catalog_for, is_visible
from dms_erp.catalog.utils import is_sellable, item_images, item_pieces_per_box, item_weight_per_box_kg
from dms_erp.pagination import clamp
from dms_erp.pricing.api import get_price_for_dealer
from dms_erp.sales import customer_po as po_files
from dms_erp.sales import dealer_roles
from dms_erp.sales.dealer_api import _assert_email_available
from dms_erp.sales.invoice_api import _has as _invoice_has
from dms_erp.sales.invoice_api import _invoice_detail
from dms_erp.sales.invoice_api import _serialize_row as _serialize_invoice_row
from dms_erp.sales.inquiry_api import _create_inquiry, _create_inquiry_multi, _inquiry_filters, _serialize as _serialize_inquiry
from dms_erp.sales.order_api import _create_order
from dms_erp.sales.order_api import _serialize as _serialize_order
from dms_erp.sales.quotation_api import _convert_to_order
from dms_erp.warehouse.utils import (
	all_batches,
	available_to_promise,
	item_codes_with_batch,
	suggest_batch_combination,
	top_batches,
	total_stock_for_item,
)


MAX_ENQUIRY_ITEMS = 50


@frappe.whitelist(methods=["GET"])
def my_catalog_export() -> str:
	"""BRD C.14.1 -- the dealer's own catalogue "shareable (e.g. PDF for WhatsApp) and in
	the dealer app": the same printable sheet staff export, limited to this dealer's own
	orderable items, and never priced -- dealers see no price before a quotation (client
	request, Oct 2026; see _catalog_entry)."""
	return _dealer_catalog_export_html(_current_dealer(), include_price=False)


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

	`price` (client reversal, Oct 2026) -- this dealer's own tier/override rate,
	same get_price_for_dealer resolution Inquiry/Quotation/Order already use, None
	if nothing resolves (empty price lists -- see pricing/api.py's own docstring on
	the fallback chain). Earlier this screen showed no price at all, on an Oct 2026
	client request ("don't show price before quotation" -- quotation was mandatory
	then). That's no longer true now that WhatsApp/the portal can both place an
	order without a quotation ever existing, so the dealer needs a starting number
	at inquiry time. This is deliberately NOT get_product's own `dealerPrice` field
	(popped below) -- that one is the flat, generic "Dealer" list rate with no
	dealer param, wrong for a Master Dealer or a dealer with a tagged override list.

	`orderable` (ISS-2026-00644) is whether this item is actually in this dealer's
	Dealer Catalog assignment (dealer_catalog_api.is_visible) -- get_catalog now shows
	every sellable item, assigned or not, so the frontend needs this flag to tell an
	orderable item apart from one it should only let the dealer inquire on."""
	# The listing reads the Item from Frappe's document cache: loading a live Item doc costs a dozen
	# queries (one per child table) and a page of 20 paid that 20 times. The detail page (every_batch)
	# still loads it live, so what a dealer sees when they open an item is never from the cache.
	product = _serialize_item(frappe.get_doc("Item", item_code) if every_batch else frappe.get_cached_doc("Item", item_code))
	own_code = next((row["customerItemCode"] for row in product["dealerCodes"] if row["dealer"] == dealer), None)
	del product["dealerCodes"]
	product.pop("dealerPrice", None)
	product["price"] = get_price_for_dealer(item_code, dealer)
	# Client finding "stock accuracy" -- a dealer only ever sees availableStockQty (damage/
	# display-bay stock already excluded), never the raw staff-facing total.
	product.pop("stockQty", None)
	product["dealerCode"] = own_code
	# The catalog list keeps the cheap top-3 preview; an item's own detail page lists
	# every batch (client request, Oct 2026: which batch the stock belongs to).
	product["topBatches"] = all_batches(item_code) if every_batch else top_batches(item_code)
	product["orderable"] = is_visible(dealer, item_code)
	return product


_CATALOG_SEARCH_FIELDS = (
	"item_name",
	"item_code",
	"custom_colour_attribute_1",
	"custom_colour_attribute_2",
	"custom_design_attribute_1",
	"custom_design_attribute_2",
)


def _catalog_search_matches(candidate_codes: set[str], search: str) -> set[str]:
	"""get_catalog's one search box now matches item name, item code OR any of the
	four colour/design attributes -- the dealer portal used to expose those as four
	separate dropdown filters (ISS-2026-00632 #4), but typing "Rustic" into the
	one search box dealers already use is a more natural fit than hunting through
	four dropdowns for it, so the dropdowns were dropped in favour of this.
	frappe.db.count doesn't accept or_filters (only get_all does), so the OR is
	resolved into an explicit name-list first, same union-of-get_all pattern as
	warehouse.inward_api.list_trucks/sales.order_api.list_orders.

	Client finding, 2 Nov 2026 -- a multi-word query used to be treated as one
	literal substring, so "black glazed" never matched "...Black 300x300 Ceramic
	Glazed Tiles" (not a contiguous substring of any single field). Each word is
	now resolved separately (OR across all fields, same as before) and the
	per-word match sets are intersected (AND across words) by narrowing
	candidate_codes on every iteration, so word order/position no longer matters.

	Client finding, 2 Nov 2026 (same round) -- item_code was missing entirely, so a
	partial code like "oak-1" never matched "DEMO-OAK-1" even though the search
	box's own placeholder ("Search item name or your code") promises code search.
	resolve_code (the exact-match fallback below, for the *whole* code) still
	covers the dealer's own private code via Item Dealer Code, which this LIKE
	search can't reach without a join -- this only extends the substring search to
	the company's own item_code, the gap the client actually hit."""
	if not candidate_codes:
		return set()
	words = [w for w in search.split() if w]
	if not words:
		return set()
	matches = set(candidate_codes)
	for word in words:
		word_matches: set[str] = set()
		for field in _CATALOG_SEARCH_FIELDS:
			word_matches |= set(
				frappe.get_all(
					"Item",
					filters={"name": ["in", list(matches)], field: ["like", f"%{word}%"]},
					pluck="name",
				)
			)
		matches = word_matches
		if not matches:
			break
	return matches


def _catalog_where(dealer: str, scope: str | None, exact: dict, search: str | None) -> tuple[str, dict]:
	"""WHERE clause (on `tabItem` aliased `i`) for the dealer's browsable catalog: every sellable
	item, split by `scope` into the dealer's own assignment ("assigned": what they can order) or
	the rest ("other": enquire only), narrowed by exact attribute filters and by `search`.
	Same rules as dealer_catalog_api.catalog_for / browsable_catalog_for: a dealer with no Dealer
	Catalog may order everything sellable; a malformed item (blank code or name) or a Pulled Back
	one is never listed. `search` is every word matched against the name, code and four colour /
	design attributes (all words must match), OR'd with an item that has stock in a batch whose
	number contains the whole search (ISS-2026-00712)."""
	from dms_erp.catalog.utils import _NOT_SELLABLE

	conds = [
		"i.item_code is not null and i.item_code != ''",
		"i.item_name is not null and i.item_name != ''",
		"ifnull(i.custom_discontinuation_status, 'Active') not in %(not_sellable)s",
	]
	params: dict = {"not_sellable": tuple(_NOT_SELLABLE), "dealer": dealer}
	assigned = "i.name in (select item from `tabDealer Catalog Item` where parent = %(dealer)s)"
	has_catalog = bool(frappe.db.exists("Dealer Catalog", dealer))
	if scope == "assigned" and has_catalog:
		conds.append(assigned)
	elif scope == "other":
		conds.append(f"not ({assigned})" if has_catalog else "1 = 0")
	for column, value in exact.items():
		if value:
			conds.append(f"i.{column} = %({column})s")
			params[column] = value
	if search and search.strip():
		words = [w for w in search.split() if w]
		field_sql = " or ".join(f"i.{f} like %(w{{n}})s" for f in _CATALOG_SEARCH_FIELDS)
		by_words = []
		for n, word in enumerate(words):
			params[f"w{n}"] = f"%{word}%"
			by_words.append("(" + field_sql.replace("{n}", str(n)) + ")")
		by_batch = item_codes_with_batch(search)
		clause = "(" + " and ".join(by_words) + ")" if by_words else "1 = 0"
		if by_batch:
			params["by_batch"] = tuple(by_batch)
			clause = f"({clause} or i.name in %(by_batch)s)"
		conds.append(clause)
	return " and ".join(conds), params


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
	own UI no longer sends them.

	ISS-2026-00712, folded into the one `search` box on client request (4 Nov
	2026 -- two separate search boxes read as one too many) -- a `search` term
	now also matches items with on-hand stock in a batch number containing it
	(warehouse.utils.item_codes_with_batch), OR'd in alongside the existing
	name/code/attribute match, not AND'd -- "RUSTIC" should find items named
	Rustic as well as items only identifiable by a batch called RUSTIC-04,
	not require both at once."""
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)

	# The set of items, its filters and its search are all resolved in SQL. This used to take every
	# sellable item code into Python, then send lists of ~4,300 codes back into three or four
	# queries per request (and again for every typed word).
	where, params = _catalog_where(
		dealer,
		scope,
		{
			"item_group": category,
			"custom_color": color,
			"custom_colour_attribute_1": colour_attribute_1,
			"custom_colour_attribute_2": colour_attribute_2,
			"custom_design_attribute_1": design_attribute_1,
			"custom_design_attribute_2": design_attribute_2,
		},
		search,
	)
	total = frappe.db.sql(f"select count(*) from `tabItem` i where {where}", params)[0][0]
	codes = frappe.db.sql(
		f"select i.name from `tabItem` i where {where} order by i.item_code asc limit %(limit)s offset %(offset)s",
		{**params, "limit": limit, "offset": offset},
		pluck=True,
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
	so both entry points actually work from the portal, not just one.

	Client report, 2 Nov 2026 -- a malformed Item (a real document whose own name
	happens to equal the searched code, but with item_code/item_name left blank —
	bad import or a record created outside the normal form) used to pass the
	sellability check below and come back as a blank, nameless "Out of stock"
	card. The final check covers both paths (this one and resolve_dealer_code
	above) the same way dealer_catalog_api._sellable_item_codes now does for
	get_catalog/browsable_catalog_for -- a real catalog entry always has both
	code and name set."""
	dealer = _current_dealer()

	product = resolve_dealer_code(dealer, code)
	if not product:
		if frappe.db.exists("Item", code):
			status = frappe.get_cached_value("Item", code, "custom_discontinuation_status") or "Active"
			if is_sellable(status):
				product = get_product(code)

	if not product or not product.get("code") or not product.get("name"):
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


@frappe.whitelist(methods=["GET"])
def get_excluded_stock(item: str) -> list[dict]:
	"""Client finding "stock accuracy" — the "View more" breakdown behind
	availableStockQty: boxes of this item sitting in a damage/insurance-claim or
	display bay, shown separately so a dealer can still see and optionally ask for
	them without them being counted as regular available stock. Same existence
	gate as get_item."""
	from dms_erp.catalog.api import get_excluded_stock as _get_excluded_stock

	dealer = _current_dealer()
	if item not in set(browsable_catalog_for(dealer)):
		frappe.throw(_("{0} is not a valid item.").format(item), frappe.PermissionError)
	return _get_excluded_stock(item)


@frappe.whitelist(methods=["GET"])
def check_recent_inquiry(item: str, damaged_stock: bool = False) -> dict:
	"""Client request, 12 Nov 2026 -- lets the portal warn a dealer ("you already
	asked about this in the last 24 hours") right where they're adding an item to
	the enquiry list, before they ever submit. A display hint only, same spirit as
	_serialize_dealer_inquiry's own `orderable` -- raise_inquiry/raise_inquiries
	still create the inquiry either way; nothing here blocks it. See
	sales.utils.recent_inquiry_for for the actual (dealer, item, damaged_stock)
	same-rolling-24-hours check, kept separate from BRD C.2.4's own 7-day one."""
	from dms_erp.sales.utils import recent_inquiry_for

	dealer = _current_dealer()
	if item not in set(browsable_catalog_for(dealer)):
		frappe.throw(_("{0} is not a valid item.").format(item), frappe.PermissionError)
	recent = recent_inquiry_for(dealer, item, damaged_stock=cint(damaged_stock))
	return {"recent": bool(recent), "inquiry": recent.name if recent else None}


@frappe.whitelist(methods=["POST"])
def raise_inquiry(
	item: str,
	qty: float,
	remarks: str | None = None,
	damaged_stock: bool = False,
	uom: str | None = None,
	entered_boxes: float | None = None,
	preferred_batch_no: str | None = None,
):
	"""BRD C.13.1 — the out-of-stock / "raise an enquiry" path. Always source="Web"
	and always unassigned (see inquiry_api._create_inquiry's own staff-only
	self-assignment default) since this is the dealer, not staff, raising it.

	ISS-2026-00678 -- `damaged_stock` is set when the dealer added this from the
	item page's damaged/insurance-claim-bay stock display rather than the regular
	"raise an enquiry" action; see inquiry_api._create_inquiry's own docstring for
	how it carries through to the eventual order and picking.

	BRD C.1.3/C.2.2, client request 2 Nov 2026 -- `uom` (Box/Pcs; see catalog.utils.
	ORDER_UOMS for the full set _create_inquiry itself accepts) lets a dealer enter
	the quantity in pieces instead of counting out boxes themselves. `entered_boxes`
	is the portal's own combined Boxes+Pcs entry (`qty` is then the Pcs component) --
	see _create_inquiry's own docstring for why that's kept separate from `uom`.

	ISS-2026-00712 -- `preferred_batch_no` is the dealer's own batch choice,
	checked against real on-hand stock the same way _create_inquiry already
	checks it; see that function's own docstring for how it carries onto the
	eventual order via convert_to_order."""
	dealer = _current_dealer()
	data = _create_inquiry(
		dealer,
		item,
		qty,
		source="Web",
		remarks=remarks,
		damaged_stock=damaged_stock,
		uom=uom,
		entered_boxes=entered_boxes,
		preferred_batch_no=preferred_batch_no,
	)
	data["orderable"] = is_visible(dealer, item)
	for item_row in data["items"]:
		item_row["available"] = _line_is_available(item_row["productId"], item_row["qty"], item_row["damagedStock"])
	# BRD client request, Oct 2026: no price shown before a quotation exists --
	# 12 Nov 2026 rebuild moved the old top-level `price` into a `prices` dict
	# (one per line), same rule, same thing to strip here.
	data.pop("prices", None)
	return data


@frappe.whitelist(methods=["POST"])
def raise_inquiries(items, remarks: str | None = None, customer_po: str | None = None) -> dict:
	"""Several items enquired on in one go (the enquiry list on the portal).
	`customer_po` (optional, 20 Nov 2026) is the dealer's own PO number, asked for on the screen that sends
	the enquiry; it is kept on the Inquiry (and carried onto a PO attached afterwards and onto an order made
	from it), never required.
	12 Nov 2026 rebuild: these all become rows on the SAME Inquiry now (BRD
	D.3) -- one real multi-item record, not N separate ones glued together by a
	`request_group` string (that field, and the whole workaround, is gone).
	`items` is a list of {item, qty}, JSON when sent as a form field. Every line
	is validated before anything is created (_create_inquiry_multi's own
	all-or-nothing guarantee), so a dealer never ends up with half a list
	raised.

	ISS-2026-00678 -- an entry may also carry `damagedStock`/`damaged_stock`
	(either key accepted) when it came from the item page's damaged/
	insurance-claim-bay stock display rather than the regular catalogue. The
	duplicate check below keys on (item, damaged_stock) together, not item alone
	-- a damaged-stock line and a regular line for the same item are two
	deliberately separate rows (see enquiry-list.ts's own docstring on why they're
	never merged), so they must not trip "listed twice" against each other. Client
	report, 2 Nov 2026: they did, because the check used to key on item only.

	Same round -- an entry may also carry `uom` (Box/Pcs), passed straight through
	to _create_inquiry per line; see raise_inquiry's own docstring. An entry may
	instead carry `enteredBoxes`/`entered_boxes` (either key accepted) for the
	portal's combined Boxes+Pcs entry -- `qty` is then just the Pcs component, which
	can legitimately be 0 on its own (boxes-only), so the zero-qty check below sums
	both rather than looking at `qty` alone.

	ISS-2026-00712 -- an entry may also carry `preferredBatchNo`/`preferred_batch_no`
	(either key accepted), the dealer's own batch choice for that line; see
	raise_inquiry's own docstring. Folded into the duplicate check's key alongside
	damaged_stock -- a dealer splitting one item's qty across two batches (the
	item page's own "Add to enquiry list" does exactly this, one line per batch)
	sends two entries for the same item, which must not trip "listed twice"
	against each other any more than a damaged-stock line does. Client report,
	4 Nov 2026: it did, because the check only accounted for damaged_stock."""
	dealer = _current_dealer()
	entries = frappe.parse_json(items) if isinstance(items, str) else items
	if not entries:
		frappe.throw(_("Add at least one item."), frappe.ValidationError)
	if len(entries) > MAX_ENQUIRY_ITEMS:
		frappe.throw(_("An enquiry can list at most {0} items.").format(MAX_ENQUIRY_ITEMS), frappe.ValidationError)

	allowed = set(browsable_catalog_for(dealer))
	seen: set[tuple[str, bool, str | None]] = set()
	for entry in entries:
		item = entry.get("item")
		if item not in allowed:
			frappe.throw(_("{0} is not a valid item.").format(item), frappe.PermissionError)
		damaged = bool(entry.get("damagedStock") or entry.get("damaged_stock"))
		preferred_batch_no = entry.get("preferredBatchNo") or entry.get("preferred_batch_no")
		key = (item, damaged, preferred_batch_no)
		if key in seen:
			frappe.throw(_("{0} is listed twice -- combine the quantities.").format(item), frappe.ValidationError)
		seen.add(key)
		entered_boxes = entry.get("enteredBoxes")
		if entered_boxes is None:
			entered_boxes = entry.get("entered_boxes")
		if flt(entry.get("qty")) + flt(entered_boxes) <= 0:
			frappe.throw(_("Enter a quantity above zero for {0}.").format(item), frappe.ValidationError)

	rows = []
	for entry in entries:
		entered_boxes = entry.get("enteredBoxes")
		if entered_boxes is None:
			entered_boxes = entry.get("entered_boxes")
		rows.append(
			{
				"item": entry["item"],
				"qty": flt(entry["qty"]),
				"damagedStock": bool(entry.get("damagedStock") or entry.get("damaged_stock")),
				"uom": entry.get("uom"),
				"enteredBoxes": flt(entered_boxes) if entered_boxes is not None else None,
				"preferredBatchNo": entry.get("preferredBatchNo") or entry.get("preferred_batch_no"),
			}
		)

	inquiry = _create_inquiry_multi(dealer, rows, source="Web", remarks=remarks)
	inquiry.pop("prices", None)
	po_number = (customer_po or "").strip()[:140]
	if po_number:
		frappe.db.set_value("Inquiry", inquiry["id"], "customer_po", po_number, update_modified=False)
	for item_row in inquiry["items"]:
		item_row["orderable"] = is_visible(dealer, item_row["productId"]) if item_row["productId"] else False
		item_row["available"] = _line_is_available(item_row["productId"], item_row["qty"], item_row["damagedStock"])
	# requestGroup kept in the response shape (now just this one Inquiry's own id)
	# for any caller still reading it after the rebuild -- real grouping is gone,
	# this is the whole enquiry now.
	return {"requestGroup": inquiry["id"], "enquiry": inquiry["id"], "items": inquiry["items"]}


def _line_is_available(item: str | None, boxes: float, damaged_stock: bool = False) -> bool:
	"""Whether `boxes` of `item` can be sold to a dealer right now -- the same number the catalog
	used to show as "in stock" (available_to_promise: damage/display bays and other dealers' open
	orders already taken off), judged against the quantity actually asked for, so a dealer is told
	"Available" / "Not available" for *their* quantity, never given a "partly available" middle.
	A damaged-stock line is a deliberate pick from the damage/claim bay, which the regular count
	never includes, so it isn't judged against it."""
	if not item:
		return False
	if damaged_stock:
		return True
	if not is_sellable(frappe.get_cached_value("Item", item, "custom_discontinuation_status") or "Active"):
		return False
	return available_to_promise(item) >= boxes


def _batch_split(item: str, boxes: float) -> list[dict]:
	"""Which batch(es) the system will draw `boxes` from, and how many boxes from each: the same
	largest-batch-first plan the warehouse reply uses (suggest_batch_combination). Only the boxes
	this dealer is asking for are returned, never a batch's own on-hand count -- batch stock is
	not something a dealer is shown any more."""
	plan = suggest_batch_combination(item, boxes)
	remaining = boxes
	split = []
	for batch in plan["batches"]:
		take = min(batch["boxes"], remaining)
		if take <= 0:
			break
		split.append({"batchNumber": batch["batchNumber"], "boxes": take})
		remaining -= take
	return split


def _sellable_boxes_now(item: str) -> float:
	"""How many boxes of `item` could be sold to a dealer right now -- the same figure
	_line_is_available compares against, returned instead of a yes/no so a short line can say how
	much of the quantity asked for there is."""
	if not is_sellable(frappe.get_cached_value("Item", item, "custom_discontinuation_status") or "Active"):
		return 0
	return max(flt(available_to_promise(item)), 0)


def _in_transit_by_item(items: set[str]) -> dict[str, dict]:
	"""{item: {boxes, eta, days, totalBoxes}} for stock already on an open, warehouse-bound Purchase
	Order. `boxes`/`eta` are the *first* arrival only (the lines due on the earliest expected date),
	not the whole pending quantity: "300 boxes in 5 days" would be untrue when only 100 of them are
	due then. `totalBoxes` is everything on order, so the screen can say that too. `days` is None
	when no PO line carries an expected date, and 0 when that date has already passed (the PO is
	late -- the dealer is told "soon", never a date in the past). Drop-ship lines are left out:
	they go straight to someone else's site and never reach Pacific's stock. One pass over the
	pending PO lines for the whole request, not one per item."""
	out: dict[str, dict] = {}
	if not items:
		return out
	from dms_erp.purchase.po_api import list_pending_po_lines

	by_item: dict[str, list[dict]] = {}
	for row in list_pending_po_lines():
		if row["itemCode"] in items and not row["dropShip"]:
			by_item.setdefault(row["itemCode"], []).append(row)
	today_date = getdate(today())
	for code, rows in by_item.items():
		dated = [r for r in rows if r["expectedReadyDate"]]
		eta = min((getdate(r["expectedReadyDate"]) for r in dated), default=None)
		first = sum(flt(r["pendingQty"]) for r in dated if getdate(r["expectedReadyDate"]) == eta) if eta else 0
		out[code] = {
			"boxes": first if eta else sum(flt(r["pendingQty"]) for r in rows),
			"eta": str(eta) if eta else None,
			"days": max((eta - today_date).days, 0) if eta else None,
			"totalBoxes": sum(flt(r["pendingQty"]) for r in rows),
		}
	return out


@frappe.whitelist(methods=["GET"])
def check_availability(items) -> list[dict]:
	"""The portal's per-line availability and batch preview, for the quantity the dealer has typed.
	`items` is a list of {item, qty (the Pcs part), enteredBoxes} -- the same entry shape
	raise_inquiries takes -- JSON when sent as a query parameter. Read-only; nothing is reserved.

	`available` still means *the whole quantity* can be supplied -- it is what the order gate and the
	inquiry rows use, unchanged, so a partly-available line is not orderable. What a short line adds
	is `availableBoxes` (how much of it there is now), `shortfallBoxes`, and `batches` for that part
	only: the dealer is told which batches the available boxes would come from instead of a bare
	"Not available". `inTransit` is stock already on an open Purchase Order and when the first of it
	is due, so a short line can also say when the rest may arrive."""
	_current_dealer()
	entries = frappe.parse_json(items) if isinstance(items, str) else items
	if not entries or len(entries) > MAX_ENQUIRY_ITEMS:
		frappe.throw(_("Send between 1 and {0} items.").format(MAX_ENQUIRY_ITEMS), frappe.ValidationError)

	transit = _in_transit_by_item({e.get("item") for e in entries if e.get("item")})
	result = []
	for entry in entries:
		item = entry.get("item")
		if not item or not frappe.db.exists("Item", item):
			frappe.throw(_("{0} is not a valid item.").format(item), frappe.PermissionError)
		entered_boxes = entry.get("enteredBoxes")
		if entered_boxes is None:
			entered_boxes = entry.get("entered_boxes")
		pieces = flt(entry.get("qty"))
		boxes = flt(entered_boxes)
		if pieces > 0:
			per_box = item_pieces_per_box(item)
			boxes += math.ceil(pieces / per_box) if per_box else 0
		elif entered_boxes is None:
			boxes = pieces
		damaged = bool(entry.get("damagedStock") or entry.get("damaged_stock"))
		available = boxes > 0 and _line_is_available(item, boxes, damaged)
		# A damaged-stock pick is not judged against the regular count, so it has no "part" either.
		available_boxes = boxes if available else (0 if damaged or boxes <= 0 else min(boxes, _sellable_boxes_now(item)))
		result.append(
			{
				"item": item,
				"boxes": boxes,
				"available": available,
				"availableBoxes": available_boxes,
				"shortfallBoxes": max(boxes - available_boxes, 0),
				"batches": _batch_split(item, available_boxes) if available_boxes > 0 and not damaged else [],
				"inTransit": transit.get(item),
			}
		)
	return result


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


def _own_inquiry_name(inquiry: str, dealer: str) -> str:
	"""The inquiry's name once it is confirmed to be this dealer's -- one error for "no such inquiry"
	and "someone else's", so a dealer cannot probe which inquiry ids exist."""
	if frappe.db.get_value("Inquiry", inquiry, "dealer") != dealer:
		frappe.throw(_("Inquiry {0} does not belong to you.").format(inquiry), frappe.PermissionError)
	return inquiry


@frappe.whitelist(methods=["POST"])
def attach_po(inquiry: str):
	"""The dealer attaches (or replaces) their purchase order on one of their own enquiries -- a
	photo or PDF, multipart with the form field "file". The same file is reused on the order made
	from this enquiry. Returns only whether a PO is on file and under what name: whatever the
	background check makes of it is for staff and is never sent here (sales.customer_po.po_state)."""
	dealer = _current_dealer()
	_own_inquiry_name(inquiry, dealer)
	if "file" not in frappe.request.files:
		frappe.throw(_("No file uploaded."), frappe.ValidationError)
	uploaded = frappe.request.files["file"]
	# One byte past the limit is read, so an oversized upload is refused without loading all of it.
	content = uploaded.stream.read(po_files.MAX_PO_BYTES + 1)
	po_files.attach_po(inquiry, uploaded.filename, content, by=frappe.session.user)
	return po_files.po_state(inquiry)


@frappe.whitelist(methods=["GET"])
def get_my_po(inquiry: str):
	"""Whether a PO is on this enquiry (and its file name) and whether the site requires one before an
	order -- all the portal needs to draw its upload box and to hold the Create order button."""
	dealer = _current_dealer()
	_own_inquiry_name(inquiry, dealer)
	return po_files.po_state(inquiry)


@frappe.whitelist(methods=["POST"])
def convert_to_order(inquiry: str, expected_dispatch, customer_po: str):
	"""Offered by the portal again (client request, 2 Nov 2026), alongside
	accept_quotation below -- BRD C.13.1 says plainly "if in stock, the dealer can
	convert the enquiry to an order, entering their own PO number to tag and close
	it," which this endpoint has always done; only the frontend button had been
	pulled for a few weeks, per the Oct 2026 "no quotation is being sent to
	dealers before order confirmation" issue (see sales/README.md's own note on
	that change). Both paths coexist now, same as this function never actually
	went away server-side: a dealer can convert an in-stock, orderable enquiry
	straight to an order here, or wait for a staff quotation and accept that
	instead (accept_quotation) -- whichever a given enquiry's situation calls for.

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
	dealer_roles.require("raise_order")
	dealer = _current_dealer()
	if not customer_po:
		frappe.throw(_("customer_po is required."), frappe.ValidationError)

	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.dealer != dealer:
		frappe.throw(_("Inquiry {0} does not belong to you.").format(inquiry), frappe.PermissionError)
	if doc.status in {"Converted to Order", "Rejected", "Closed"}:
		frappe.throw(_("Inquiry {0} is {1} and can no longer be converted.").format(inquiry, doc.status), frappe.ValidationError)
	# No PO, no order -- only when the site has switched require_po_attachment on, and never
	# conditional on what the background check made of the PO (see sales.customer_po).
	po_files.assert_po_attached(doc)

	# 12 Nov 2026 rebuild (BRD D.3) -- one Inquiry can hold several items now;
	# every real (non-Custom-Style) row becomes one line on a single combined
	# Sales Order, same as native Desk's inquiry_api.make_order already does.
	real_rows = [row for row in doc.items if row.item]
	if not real_rows:
		# ISS-2026-00636 -- every row is a Custom Style request (no real Item):
		# there's nothing to build a Sales Order line from.
		from dms_erp.sales.utils import dealer_contact_line

		frappe.throw(
			_("This is a custom-style enquiry with no catalog item yet -- it can't be converted to an order. {0}").format(
				dealer_contact_line(dealer)
			),
			frappe.ValidationError,
		)
	not_orderable = [row.item for row in real_rows if not is_visible(dealer, row.item)]
	if not_orderable:
		from dms_erp.sales.utils import dealer_contact_line

		frappe.throw(
			_("{0} isn't yet set up for ordering on your account. {1}").format(", ".join(not_orderable), dealer_contact_line(dealer)),
			frappe.PermissionError,
		)

	# A line the dealer asked for more of than is on hand can't become an order: the portal hides the
	# button, and this is the real gate. They can still ask for a quotation instead.
	unavailable = [row.item for row in real_rows if not _line_is_available(row.item, row.qty, bool(row.get("damaged_stock")))]
	if unavailable:
		frappe.throw(
			_("{0} is not available in the quantity asked for, so this enquiry can't be turned into an order. You can request a quotation instead.").format(
				", ".join(unavailable)
			),
			frappe.ValidationError,
		)

	# The dealer's order waits for Pacific: one draft per enquiry, so a second tap (or a second device)
	# cannot raise a duplicate while the first is being reviewed.
	if frappe.db.exists("Sales Order", {"custom_source_type": "Inquiry", "custom_source_ref": inquiry, "docstatus": 0}):
		frappe.throw(_("You have already sent an order for this enquiry. Pacific is reviewing it."), frappe.ValidationError)

	# ISS-2026-00712 -- carries each row's own batch choice (if any) from the
	# enquiry onto its resulting order line; _priced_order_line re-validates it
	# against real on-hand stock (it may have moved since the enquiry).
	lines = [{"item": row.item, "qty": row.qty, "preferred_batch_no": row.get("preferred_batch_no")} for row in real_rows]
	from dms_erp.sales.utils import default_billing_shipping_for

	customer_address, shipping_address_name = default_billing_shipping_for(dealer)
	try:
		order = _run_as_system(
			lambda: _create_order(
				dealer,
				lines,
				expected_dispatch,
				customer_address,
				shipping_address_name,
				inquiry,
				customer_po=customer_po,
				# Same bill-to-address GST rule as the staff builder (sales.utils.resolve_tax_for_dealer).
				taxes_and_charges="auto",
				# 20 Nov 2026: what a dealer sends is a request, not a confirmed order. It is saved as a
				# DRAFT Sales Order -- no stock reserved, nothing dispatched -- and Pacific reviews it
				# (and the PO attached to it) and submits it from the staff app (order_api.submit_draft_order),
				# which re-runs every gate (credit limit, channel, duplicate PO) exactly as for a staff order.
				draft=True,
			)
		)
		# Marks this draft as the dealer's own: it is the only kind of draft the dealer ever sees
		# (a draft staff are working on is never theirs -- see _is_dealer_draft).
		frappe.db.set_value("Inquiry", inquiry, "linked_sales_order", order["id"], update_modified=False)
		return dealer_roles.price_filtered(order)
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


# 12 Nov 2026 rebuild (BRD D.3) -- "enquiry" used to mean "one or more Inquiry
# records sharing a request_group", gathered back into one view by
# _serialize_enquiry/_enquiry_for below, because the old Inquiry doctype was
# one item per record. Now one Inquiry record already holds every item the
# dealer asked about together -- an "enquiry" and an "Inquiry" are the same
# thing. The enquiry-named endpoints stay (the frontend still calls them) but
# are now thin wrappers over the plain Inquiry ones; request_group/_enquiry_for/
# _serialize_enquiry/_enquiry_status are gone.


@frappe.whitelist(methods=["GET"])
def list_my_enquiries(status: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0):
	return list_my_inquiries(status=status, search=search, limit=limit, offset=offset)


@frappe.whitelist(methods=["GET"])
def get_my_enquiry(enquiry: str) -> dict:
	return get_my_inquiry(enquiry)


@frappe.whitelist(methods=["POST"])
def request_quotation(enquiry: str) -> dict:
	"""Client request, 28 Oct 2026 -- the dealer portal's own "Ask for quotation" button on
	an enquiry still waiting on one. Before this, sending an enquiry left the dealer with
	nothing to do but passively wait for staff to get to it (the "waiting" card on
	inquiries.$id.tsx was read-only). 12 Nov 2026 rebuild: `enquiry` is just the Inquiry's
	own name now (no more group id) -- sets dealer_requested_quote (+ timestamp) once, on
	the whole Inquiry, not per item (quoting is now also a whole-Inquiry action -- see
	quotation_api.create_quotation's `inquiries` closing out the full record, not a line).

	Deliberately idempotent and silent on an enquiry with nothing left to flag (already
	quoted, converted, rejected or closed) rather than throwing -- the portal only shows this
	button while still waiting, so a flag-nothing call here means the dealer's own screen was
	stale, not a real error worth surfacing."""
	dealer = _current_dealer()
	doc = frappe.get_doc("Inquiry", enquiry)
	if doc.dealer != dealer:
		frappe.throw(_("Enquiry {0} does not belong to you.").format(enquiry), frappe.PermissionError)

	terminal = {"Converted to Order", "Rejected", "Closed"}
	if doc.linked_quotation or doc.status in terminal:
		return {"flagged": 0}
	doc.dealer_requested_quote = 1
	doc.dealer_requested_quote_at = now_datetime()
	doc.save(ignore_permissions=True)
	return {"flagged": 1}


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
	a display hint, re-checked there.

	12 Nov 2026 rebuild -- orderable/productName/images move from one top-level
	set of keys to per-item, since an Inquiry can hold several items now; each
	entry in `items[]` gets its own. Client request, 2 Nov 2026 (the images
	part) -- the enquiry detail screen (inquiries.$id.tsx) showed every item
	with no image at all, unlike the catalog/item-detail pages."""
	data = _serialize_inquiry(doc)
	for row, item_row in zip(doc.items, data["items"]):
		item_row["orderable"] = bool(row.item) and is_visible(dealer, row.item)
		item_row["available"] = _line_is_available(row.item, row.qty, bool(row.get("damaged_stock")))
		item_row["productName"] = frappe.get_cached_value("Item", row.item, "item_name") if row.item else None
		item_row["images"] = item_images(row.item) if row.item else []
	# inquiry_api's own serializer already set `prices` via the same
	# get_price_for_dealer resolution _catalog_entry now uses (client reversal,
	# Oct 2026, see _catalog_entry's docstring) -- kept, for a person whose role may see prices.
	return dealer_roles.price_filtered(data)


# Client finding ISS-2026 "only show pending orders" (sheet item), refined 27 Oct 2026 --
# the dealer portal's Orders screen collapses order_api.ORDER_STAGES' 6 stages down to 2
# tabs: Open (Confirmed/Picking/Ready to Dispatch/Dispatched) and Cancelled. No separate
# "Pending" tab and no "All" tab -- a just-placed order is Open from the start, same as one
# already being picked.
#
# Delivered is deliberately in NEITHER tab -- a client-confirmed gap, not an oversight: once
# an order is Delivered it has no tab of its own, and the Sales Invoice (once one exists) is
# meant to be the dealer's record of it instead. That invoice is not automatic, though --
# create_sales_invoice is a separate, manually-triggered staff action, so a Delivered order
# can genuinely sit uninvoiced (and therefore invisible on this screen) for a while. Accepted
# deliberately; revisit if that proves confusing in practice.
OPEN_ORDER_STAGES = ("Confirmed", "Picking", "Ready to Dispatch", "Dispatched")


@frappe.whitelist(methods=["GET"])
def list_my_orders(stage: str | None = None, stage_group: str | None = None, limit: int = 20, offset: int = 0):
	dealer_roles.require("view_orders")
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)
	# A draft staff are working on is never shown to the dealer; the dealer's own sent order is (it waits for
	# Pacific's confirmation, see convert_to_order). It counts as open, and has no other stage to filter by.
	values: dict = {"dealer": dealer, "limit": limit, "offset": offset}
	submitted = "so.docstatus != 0"
	if stage:
		values["stage"] = stage
		include_drafts = stage == "Draft"
		submitted = "(so.docstatus != 0 and so.custom_fulfillment_stage = %(stage)s)"
	elif stage_group == "open":
		submitted = "(so.docstatus != 0 and so.custom_fulfillment_stage in %(open_stages)s)"
		values["open_stages"] = OPEN_ORDER_STAGES
		include_drafts = True
	else:
		include_drafts = True
	draft = (
		"(so.docstatus = 0 and exists (select 1 from `tabInquiry` i where i.name = so.custom_source_ref and i.linked_sales_order = so.name))"
	)
	visible = f"({submitted} or {draft})" if include_drafts else submitted
	where = "so.customer = %(dealer)s and " + visible
	total = frappe.db.sql(f"select count(*) from `tabSales Order` so where {where}", values)[0][0]
	names = frappe.db.sql(
		f"select so.name from `tabSales Order` so where {where} order by so.creation desc limit %(limit)s offset %(offset)s",
		values,
		pluck=True,
	)
	return {
		"items": [_dealer_order(frappe.get_doc("Sales Order", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


# Internal to Pacific, never shown to a dealer: the retail/bulk channel (an internal
# classification, already kept off the dealer's quotation), the staff owner's and every
# stage-changer's user/email, the advance-payment sign-off and the reservation-review flags.
_INTERNAL_ORDER_KEYS = ("channel", "owner", "advanceConfirmed", "advance", "reservation", "deliveryNotes")


def _is_dealer_draft(doc) -> bool:
	"""A draft Sales Order that is this dealer's own request (made by convert_to_order, which links it from its
	Inquiry) rather than a draft staff are preparing -- the only draft a dealer may see."""
	return bool(
		doc.docstatus == 0
		and doc.get("custom_source_type") == "Inquiry"
		and frappe.db.get_value("Inquiry", doc.get("custom_source_ref"), "linked_sales_order") == doc.name
	)


def _dealer_order(doc) -> dict:
	"""order_api's staff serialization with Pacific-internal fields removed -- the
	portal used to receive the staff shape as-is, including staff email addresses.
	deliveryNotes (BRD C.3.1) is stripped too -- its warehouse/bay names are internal
	warehouse layout, nothing a dealer needs; the order's own `stage` already tells
	them whether it has shipped."""
	order = _serialize_order(doc)
	for key in _INTERNAL_ORDER_KEYS:
		order.pop(key, None)
	order["history"] = [{k: v for k, v in event.items() if k != "by"} for event in order.get("history") or []]
	return dealer_roles.price_filtered(order)


@frappe.whitelist(methods=["GET"])
def get_my_order(order: str) -> dict:
	dealer_roles.require("view_orders")
	dealer = _current_dealer()
	doc = frappe.get_doc("Sales Order", order)
	if doc.customer != dealer or (doc.docstatus == 0 and not _is_dealer_draft(doc)):
		frappe.throw(_("Order {0} does not belong to you.").format(order), frappe.PermissionError)
	return _dealer_order(doc)


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
				# Client request, 12 Nov 2026 -- see catalog.utils.item_images' own docstring.
				"images": item_images(row.item_code),
				"qty": row.qty,
				"priceListRate": row.price_list_rate,
				"discountPercentage": row.discount_percentage,
				"rate": row.rate,
				"amount": row.amount,
				"deliveryDate": row.get("custom_delivery_date"),
				"weightPerBoxKg": weight,
				"totalWeightKg": weight * row.qty if weight is not None else None,
				# ISS-2026-00712 -- the dealer's own batch choice, carried over from
				# whichever Inquiry line this quotation was built from.
				"preferredBatchNo": row.get("custom_preferred_batch_no"),
			}
		)
	return dealer_roles.price_filtered({
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
	})


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
	dealer_roles.require("view_quotations")
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
	dealer_roles.require("view_quotations")
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

	dealer_roles.require("raise_order")
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
		return dealer_roles.price_filtered(
			_run_as_system(
				lambda: _convert_to_order(
					quotation, expected_dispatch=expected_dispatch, customer_po=customer_po.strip(), ignore_permissions=True
				)
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
	dealer_roles.require("view_invoices")
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
		"items": [dealer_roles.price_filtered(_serialize_invoice_row(frappe.get_doc("Sales Invoice", name))) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_my_invoice(invoice: str) -> dict:
	dealer_roles.require("view_invoices")
	dealer = _current_dealer()
	doc = frappe.get_doc("Sales Invoice", invoice)
	if doc.customer != dealer or doc.docstatus != 1:
		frappe.throw(_("Invoice {0} does not belong to you.").format(invoice), frappe.PermissionError)
	return dealer_roles.price_filtered(_invoice_detail(doc))


@frappe.whitelist(methods=["POST"])
def cancel_my_order(order: str, note: str | None = None) -> dict:
	from frappe.utils import get_datetime, now_datetime, time_diff_in_hours

	from dms_erp.approvals.api import gate_authorized_action
	from dms_erp.sales.order_api import _cancel_order_action

	dealer = _current_dealer()
	doc = frappe.get_doc("Sales Order", order)
	if doc.customer != dealer:
		frappe.throw(_("Order {0} does not belong to you.").format(order), frappe.PermissionError)

	stage = doc.custom_fulfillment_stage
	if stage in ("Delivered", "Cancelled"):
		frappe.throw(_("Order {0} can no longer be cancelled (currently {1}).").format(order, stage), frappe.ValidationError)

	# Client request, 12 Nov 2026 -- the self-cancel window now counts from
	# midnight of the order's own Transaction Date (Sales Order's own name for
	# what Sales Invoice calls Posting Date), not the exact real-world moment it
	# was created (doc.creation). Transaction Date has no time of its own, so
	# get_datetime() on it is midnight of that date -- a Sales Order posted
	# today, entered at 11pm, still gets the same 24-hour window as one entered
	# at 9am the same day, rather than effectively less because of when in the
	# day it happened to be created.
	window_start = get_datetime(doc.transaction_date)
	age_hours = time_diff_in_hours(now_datetime(), window_start)
	if age_hours <= DEALER_CANCEL_WINDOW_HOURS:
		doc.custom_fulfillment_stage = "Cancelled"
		doc.append(
			"custom_stage_history",
			{"stage": "Cancelled", "at": now_datetime(), "by": frappe.session.user, "note": note or "Cancelled by dealer"},
		)
		doc.save(ignore_permissions=True)
		return {"cancelled": True, "approvalRequired": False, "order": _dealer_order(doc)}

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
		# So the portal can hide what this person may not use instead of showing a button that only
		# ever refuses (the server still checks every call).
		"role": dealer_roles.role_for_user(),
		"permissions": dealer_roles.permissions_for_user(),
		# The portal prompts after every OTP login until this is true, so the dealer
		# can sign in with email + password instead of waiting on an OTP each time.
		# Read off the session user (the account that would log in with it), the same
		# __Auth check sales.dealer_api._portal_status_for uses for staff.
		"hasPassword": bool(
			frappe.db.sql(
				"select 1 from `__Auth` where doctype='User' and fieldname='password' and name=%s",
				(frappe.session.user,),
			)
		),
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
