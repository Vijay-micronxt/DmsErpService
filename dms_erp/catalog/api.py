"""Product / Item Master (BRD §6).

Item is the native ERPNext equivalent of the frontend's Product — category maps to
Item Group, leadTimeDays to Item's own lead_time_days, altItemId to the native Item
Alternative doctype (two_way=1). Only size/finish/color/series/swatch/pieces-per-box/
sqft-per-box/weight-per-box/discontinuation-status have no ERPNext equivalent, so
those (and only those) are Custom Fields (see catalog/setup.py).

stockQty is read from ERPNext's Bin (Phase 3: Warehouse, added once that phase
landed). `bay` and `lastSoldDays` are still stubbed placeholders — `bay` because a
real item can be split across several bays (a flat singular field can't represent
that; see warehouse/stock_api.py for the real per-bay breakdown), `lastSoldDays`
because it needs Sales history (Phase 5).

Creating/editing item masters and publishing a launch price are Purchase/Management
actions per the BRD flow; Sales/Warehouse only read the catalog.

BRD C.1.1 requires every item to be created against a Series master — create_product
enforces that going forward. It's deliberately enforced only on creation, not on
update_product: a handful of items created before this rule existed have no Series
attached (grandfathered), and forcing a retroactive choice on every edit of one of
those is a separate decision nobody's made yet.

custom_series (the free-text label, e.g. "Marbella") is never itself a caller-supplied
param on create_product or update_product's patch -- it's always derived from
custom_series_ref, the same way ERPNext's own "fetch_from" convention keeps a
link-derived display field in sync with its master. Letting a caller set it
independently would let it silently drift from the Series it's supposedly labeling.

hsnCode is a third category, alongside "native ERPNext field" and "our own Custom
Field": it's neither — gst_hsn_code only exists on Item when the india_compliance
app (GST/India tax compliance) is installed on a given site, which mandates it on
every Item via its own validate hook. Passed straight through as optional here
(getattr'd defensively on read) so a site with india_compliance installed can supply
it, without requiring it or assuming its existence on a site without that app.

list_products and list_item_groups are paginated (`limit`/`offset`, via dms_erp.
pagination.clamp) and return `{"items", "total", "limit", "offset"}`, not a bare
list -- reports need the whole result set, not a page of it, so they call
list_all_products (unpaginated, internal-only) instead of the whitelisted endpoint.
"""

import difflib
import re

import frappe
from frappe import _

from dms_erp.catalog.utils import DISCONTINUATION_STATUSES, is_reorderable, is_sellable, item_default_supplier
from dms_erp.pagination import clamp
from dms_erp.pricing import api as pricing_api
from dms_erp.warehouse.utils import total_stock_for_item

CATALOG_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}
DEFAULT_STOCK_UOM = "Box"


def _assert_can_manage_products():
	if not set(frappe.get_roles(frappe.session.user)) & CATALOG_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage the item master."), frappe.PermissionError)


def _get_alt_item(item_code: str) -> str | None:
	row = frappe.db.get_value("Item Alternative", {"item_code": item_code}, "alternative_item_code")
	if row:
		return row
	return frappe.db.get_value(
		"Item Alternative", {"alternative_item_code": item_code, "two_way": 1}, "item_code"
	)


def _set_alt_item(item_code: str, alt_item_code: str | None):
	existing = frappe.get_all(
		"Item Alternative",
		or_filters={"item_code": item_code, "alternative_item_code": item_code},
		pluck="name",
	)
	for name in existing:
		frappe.delete_doc("Item Alternative", name, ignore_permissions=True)

	if alt_item_code:
		frappe.get_doc(
			{
				"doctype": "Item Alternative",
				"item_code": item_code,
				"alternative_item_code": alt_item_code,
				"two_way": 1,
			}
		).insert(ignore_permissions=True)


def _apply_series_defaults(series_ref, finish, pieces_per_box, sqft_per_box, weight_per_box_kg):
	"""BRD C.1.1: a Series is the central master an Item is created against.
	Explicit attribute values passed to create_product still win over the Series'
	own — a Series only fills in what the caller left unset (falsy) for those.
	The series label itself and both thresholds have no caller-supplied override
	at all: they always come straight from the Series, so the label can never
	drift out of sync with the master it's linked to (mirrors ERPNext's own
	"fetch_from" convention for link-derived display fields)."""
	bulk_qty_threshold = 0
	retail_qty_threshold = 0
	series_label = None
	if series_ref:
		series_doc = frappe.get_doc("Product Series", series_ref)
		finish = finish or series_doc.finish
		series_label = series_doc.series_name
		pieces_per_box = pieces_per_box or series_doc.pieces_per_box
		sqft_per_box = sqft_per_box or series_doc.sqft_per_box
		weight_per_box_kg = weight_per_box_kg or series_doc.weight_per_box_kg
		bulk_qty_threshold = series_doc.bulk_qty_threshold
		retail_qty_threshold = series_doc.retail_qty_threshold
	return finish, series_label, pieces_per_box, sqft_per_box, weight_per_box_kg, bulk_qty_threshold, retail_qty_threshold


def _serialize_dealer_code(row) -> dict:
	return {"dealer": row.dealer, "customerItemCode": row.customer_item_code, "sampleIssued": bool(row.sample_issued)}


def _serialize_image(row) -> dict:
	return {"image": row.image, "imageType": row.image_type, "isPrimary": bool(row.is_primary)}


def _serialize(item_doc: "frappe.model.document.Document") -> dict:
	status = item_doc.custom_discontinuation_status or "Active"
	return {
		"id": item_doc.name,
		"code": item_doc.item_code,
		"name": item_doc.item_name,
		"size": item_doc.custom_size,
		"finish": item_doc.custom_finish,
		"color": item_doc.custom_color,
		"series": item_doc.custom_series,
		"seriesRef": item_doc.custom_series_ref,
		"bulkQtyThreshold": item_doc.custom_bulk_qty_threshold,
		"retailQtyThreshold": item_doc.custom_retail_qty_threshold,
		"category": item_doc.item_group,
		"swatch": item_doc.custom_swatch_color,
		# Total on-hand qty across every bay, now that Phase 3 (Warehouse) exists. `bay`
		# stays a placeholder — a real item can be split across several bays (see
		# stock_api.list_stock for the per-bay breakdown), which this flat singular
		# field from the frontend's Product type can't represent on its own.
		"stockQty": total_stock_for_item(item_doc.name),
		"bay": "—",
		# Last-sold comes from Phase 5 (Sales) — stubbed for now.
		"lastSoldDays": 0,
		"dealerPrice": pricing_api.get_dealer_price(item_doc.name),
		"status": status,
		"isReorderable": is_reorderable(status),
		"isSellable": is_sellable(status),
		"piecesPerBox": item_doc.custom_pieces_per_box,
		"sqftPerBox": item_doc.custom_sqft_per_box,
		"weightPerBoxKg": item_doc.custom_weight_per_box_kg,
		# BRD D.2 -- the item's own custom_default_supplier if set, else its Series'
		# supplier (see catalog.utils.item_default_supplier).
		"defaultSupplier": item_default_supplier(item_doc.name),
		"leadTimeDays": item_doc.lead_time_days,
		"altItemId": _get_alt_item(item_doc.name),
		"dealerCodes": [_serialize_dealer_code(row) for row in item_doc.custom_dealer_codes],
		"images": [_serialize_image(row) for row in item_doc.custom_images],
		# gst_hsn_code isn't a dms_erp field at all -- it's added to Item by the
		# india_compliance app when installed (mandatory there for GST invoicing
		# on Indian sites). getattr() rather than direct access since the field
		# simply doesn't exist in the doc's meta on a site without that app.
		"hsnCode": getattr(item_doc, "gst_hsn_code", None),
	}


def _product_filters(
	dealer: str | None,
	search: str | None,
	category: str | None = None,
	status: str | None = None,
	supplier: str | None = None,
) -> dict:
	from dms_erp.catalog.dealer_catalog_api import catalog_for

	filters = {}
	if search:
		filters["item_name"] = ["like", f"%{search}%"]
	if category:
		filters["item_group"] = category
	if status:
		filters["custom_discontinuation_status"] = status

	# dealer and supplier can only be expressed as explicit item-code lists (dealer
	# via catalog_for; supplier via Item Price Proposal -- name==item, autoname
	# "field:item", so plucking Item Price Proposal.name for that supplier already
	# gives item codes directly, no join needed). Both given at once must intersect,
	# not overwrite one another -- an empty result is a real "nothing matches", not
	# "no filter", so it's given a sentinel that matches no real item code rather
	# than an empty ["in", []] (Frappe doesn't guarantee that shape short-circuits).
	id_restrictions = []
	if dealer:
		id_restrictions.append(set(catalog_for(dealer)))
	if supplier:
		id_restrictions.append(set(frappe.get_all("Item Price Proposal", filters={"supplier": supplier}, pluck="name")))
	if id_restrictions:
		filters["name"] = ["in", list(set.intersection(*id_restrictions)) or [""]]

	return filters


def list_all_products(
	dealer: str | None = None,
	search: str | None = None,
	category: str | None = None,
	status: str | None = None,
	supplier: str | None = None,
) -> list[dict]:
	"""Unpaginated -- for internal callers (reports) that need the full result set,
	not a page of it. list_products (the whitelisted endpoint) is the paginated one."""
	filters = _product_filters(dealer, search, category, status, supplier)
	codes = frappe.get_all("Item", filters=filters, pluck="name", order_by="item_code asc")
	return [_serialize(frappe.get_doc("Item", code)) for code in codes]


@frappe.whitelist(methods=["GET"])
def list_products(
	dealer: str | None = None,
	search: str | None = None,
	category: str | None = None,
	status: str | None = None,
	supplier: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	limit, offset = clamp(limit, offset)
	filters = _product_filters(dealer, search, category, status, supplier)
	total = frappe.db.count("Item", filters=filters)
	codes = frappe.get_all(
		"Item", filters=filters, pluck="name", order_by="item_code asc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Item", code)) for code in codes],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_product(item: str):
	return _serialize(frappe.get_doc("Item", item))


@frappe.whitelist(methods=["POST"])
def create_product(
	code: str,
	name: str,
	category: str,
	supplier: str,
	purchase_cost: float,
	margin_pct: float,
	effective_date,
	series_ref: str | None = None,
	size: str | None = None,
	finish: str | None = None,
	color: str | None = None,
	swatch: str | None = None,
	status: str = "Active",
	pieces_per_box: float = 0,
	sqft_per_box: float = 0,
	weight_per_box_kg: float = 0,
	# BRD D.2 — independent of `supplier` above (which is only who this launch price came
	# from): lets Purchase name a different default supplier at creation time, e.g. when
	# the launch quote and the intended ongoing source aren't the same company. Falls back
	# to `supplier` when not given, same as before this param existed.
	default_supplier: str | None = None,
	lead_time_days: int = 0,
	alt_item: str | None = None,
	hsn_code: str | None = None,
):
	_assert_can_manage_products()

	if status not in DISCONTINUATION_STATUSES:
		frappe.throw(_("Invalid status: {0}").format(status), frappe.ValidationError)

	# BRD C.1.1 — every new item must be created against a Series master; only existing
	# items created before this was enforced are grandfathered without one (see the
	# module docstring). update_product still allows leaving seriesRef unset when
	# editing one of those, since retroactively forcing a choice there is a separate,
	# not-yet-made call.
	if not series_ref or not frappe.db.exists("Product Series", series_ref):
		frappe.throw(_("A Series master is required to create a new item."), frappe.ValidationError)

	# The series label isn't a caller-supplied param at all -- it's always derived from the
	# Series master itself, so it can never drift out of sync with the link (see
	# _apply_series_defaults). Since series_ref is mandatory above, series_label is always set.
	finish, series_label, pieces_per_box, sqft_per_box, weight_per_box_kg, bulk_qty_threshold, retail_qty_threshold = _apply_series_defaults(
		series_ref, finish, pieces_per_box, sqft_per_box, weight_per_box_kg
	)

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": name,
			"item_group": category,
			"stock_uom": DEFAULT_STOCK_UOM,
			"is_stock_item": 1,
			"custom_size": size,
			"custom_finish": finish,
			"custom_color": color,
			"custom_series": series_label,
			"custom_series_ref": series_ref,
			"custom_bulk_qty_threshold": bulk_qty_threshold,
			"custom_retail_qty_threshold": retail_qty_threshold,
			"custom_swatch_color": swatch,
			"custom_discontinuation_status": status,
			"custom_pieces_per_box": pieces_per_box,
			"custom_sqft_per_box": sqft_per_box,
			"custom_weight_per_box_kg": weight_per_box_kg,
			# BRD D.2 -- defaults to the launch-pricing supplier when default_supplier isn't
			# given explicitly, so every item created through this endpoint always gets one;
			# Purchase can still re-source it later via update_product's "defaultSupplier" patch.
			"custom_default_supplier": default_supplier or supplier,
			"lead_time_days": lead_time_days,
			# Only meaningful (and only mandatory) when india_compliance is
			# installed -- harmless to set on a site without it (Frappe just
			# ignores a value for a field that doesn't exist in the doctype's
			# meta on that site).
			"gst_hsn_code": hsn_code,
		}
	)
	item.insert(ignore_permissions=True)

	if alt_item:
		_set_alt_item(item.name, alt_item)

	# Seeds a Pending price proposal for the launch team to approve — the dealer price
	# only goes live once Purchase/Management calls pricing.approve_price (BRD §7.4),
	# it is never set directly from the item-master form.
	pricing_api.ensure_price_record(item.name, supplier, purchase_cost, margin_pct, effective_date)

	return _serialize(item)


@frappe.whitelist(methods=["POST", "PUT"])
def update_product(item: str, patch: dict):
	_assert_can_manage_products()

	if "status" in patch and patch["status"] not in DISCONTINUATION_STATUSES:
		frappe.throw(_("Invalid status: {0}").format(patch["status"]), frappe.ValidationError)

	# "series" (the display label) is deliberately not in this map -- like create_product, it's
	# never caller-settable directly, only ever derived from seriesRef below, so it can't drift
	# out of sync with the master it's linked to.
	field_map = {
		"name": "item_name",
		"category": "item_group",
		"size": "custom_size",
		"finish": "custom_finish",
		"color": "custom_color",
		"swatch": "custom_swatch_color",
		"status": "custom_discontinuation_status",
		"piecesPerBox": "custom_pieces_per_box",
		"sqftPerBox": "custom_sqft_per_box",
		"weightPerBoxKg": "custom_weight_per_box_kg",
		"leadTimeDays": "lead_time_days",
		"hsnCode": "gst_hsn_code",
		"dealerCodes": "custom_dealer_codes",
		"images": "custom_images",
		"seriesRef": "custom_series_ref",
		"bulkQtyThreshold": "custom_bulk_qty_threshold",
		"retailQtyThreshold": "custom_retail_qty_threshold",
		"defaultSupplier": "custom_default_supplier",
	}

	if "seriesRef" in patch and patch["seriesRef"] and not frappe.db.exists("Product Series", patch["seriesRef"]):
		frappe.throw(_("Unknown Series master: {0}").format(patch["seriesRef"]), frappe.ValidationError)

	doc = frappe.get_doc("Item", item)
	for key, value in patch.items():
		if key == "altItemId":
			_set_alt_item(doc.name, value)
			continue
		fieldname = field_map.get(key)
		if fieldname:
			doc.set(fieldname, value)

	if "seriesRef" in patch and patch["seriesRef"]:
		doc.custom_series = frappe.db.get_value("Product Series", patch["seriesRef"], "series_name")

	doc.save(ignore_permissions=True)

	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def upload_product_image(item: str, image_type: str = "Product", is_primary: str | int | bool = False):
	"""BRD D.4 item image gallery — multipart upload (form field "file"), saved as a
	real Frappe File attached to the Item, then appended as a new row on
	custom_images. update_product's images patch already lets a caller replace the
	whole gallery at once with URLs it already has; this is the missing write path
	that actually gets a browser-picked file onto the server and into a URL in the
	first place, one row at a time rather than round-tripping the whole array."""
	_assert_can_manage_products()

	if "file" not in frappe.request.files:
		frappe.throw(_("No file uploaded."), frappe.ValidationError)
	if image_type not in {"Product", "Application", "Additional"}:
		frappe.throw(_("Invalid image_type: {0}").format(image_type), frappe.ValidationError)

	from frappe.utils.file_manager import save_file

	uploaded = frappe.request.files["file"]
	file_doc = save_file(uploaded.filename, uploaded.stream.read(), "Item", item, is_private=0)

	# multipart/form-data always arrives as a string (not JSON-typed) -- parsed
	# leniently rather than relying on frappe's whitelist arg coercion for this.
	is_primary_flag = str(is_primary).strip().lower() in {"1", "true", "yes"}

	doc = frappe.get_doc("Item", item)
	doc.append("custom_images", {"image": file_doc.file_url, "image_type": image_type, "is_primary": 1 if is_primary_flag else 0})
	doc.save(ignore_permissions=True)

	return _serialize(doc)


@frappe.whitelist(methods=["POST", "DELETE"])
def remove_product_image(item: str, image: str):
	"""Drops one row from the gallery by its file URL — the uploaded File document
	itself is left alone (same "detach, don't delete the underlying file" choice
	Frappe's own attachment UI makes) since other records could in principle still
	reference it."""
	_assert_can_manage_products()

	doc = frappe.get_doc("Item", item)
	doc.custom_images = [row for row in doc.custom_images if row.image != image]
	doc.save(ignore_permissions=True)

	return _serialize(doc)


@frappe.whitelist(methods=["GET"])
def resolve_dealer_code(dealer: str, code: str) -> dict | None:
	"""BRD C.1.5/D.4: resolve a dealer's own customer_item_code back to the Item it
	refers to. The hard prerequisite for the WhatsApp flow and for dealer-app catalog
	gating to mean what the BRD says — a dealer only ever types/scans their own code,
	never the internal item_code. Returns None (not a throw) when the code doesn't
	resolve for that dealer, since "not found" is an expected, routine outcome here
	(a mistyped code), not an error condition."""
	item_code = frappe.db.get_value("Item Dealer Code", {"dealer": dealer, "customer_item_code": code}, "parent")
	if not item_code:
		return None
	return _serialize(frappe.get_doc("Item", item_code))


_MENTION_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")
# A single token like "GVT6013" (no hyphen, no space -- typed as one word) has no
# natural split point to recover "GVT-6013" from except this common item-code shape:
# a letter run immediately followed by a digit run.
_LETTER_DIGIT_BOUNDARY_RE = re.compile(r"^([A-Za-z]+)(\d[\dA-Za-z]*)$")


def _candidate_variants(s: str) -> set[str]:
	# Dealer codes are conventionally uppercase, but a message typed on a phone
	# keyboard is not -- try both rather than requiring the dealer to match case.
	return {s, s.upper()}


@frappe.whitelist(methods=["GET"])
def resolve_item_mention(dealer: str, text: str) -> dict | None:
	"""Finds a dealer's own item code somewhere inside a free-text message (a WhatsApp
	enquiry, not a bare code lookup like resolve_dealer_code above) — e.g. "GVT 6013
	stock hai kya" should resolve the same as if the dealer had typed "GVT-6013" or
	"GVT6013" alone. Deterministic substring matching only, no fuzzy/NLU matching:
	dealer codes are exact identifiers the dealer chose, not natural language, so
	exact-match candidates generated from the raw text are the right tool here —
	unlike *whether* a message is an availability question at all, which is a
	language-understanding problem this function deliberately doesn't attempt (see
	comms/api.py's inbound-webhook handling for that side of it).

	Returns None (not a throw) when nothing resolves, same convention as
	resolve_dealer_code — "no code found in this text" is routine, not an error."""
	tokens = _MENTION_TOKEN_RE.findall(text or "")
	if not tokens:
		return None

	candidates: list[str] = []
	# Longer, more specific candidates first (adjacent-token pairs, joined the ways a
	# dealer code might actually be split across words: hyphenated, concatenated, or
	# space-separated), then fall back to single tokens.
	for i in range(len(tokens) - 1):
		a, b = tokens[i], tokens[i + 1]
		for pair in (f"{a}-{b}", f"{a}{b}", f"{a} {b}"):
			candidates.extend(_candidate_variants(pair))
	for token in tokens:
		candidates.extend(_candidate_variants(token))
		boundary = _LETTER_DIGIT_BOUNDARY_RE.match(token)
		if boundary:
			candidates.extend(_candidate_variants(f"{boundary.group(1)}-{boundary.group(2)}"))

	seen: set[str] = set()
	for candidate in candidates:
		if candidate in seen:
			continue
		seen.add(candidate)
		item = resolve_dealer_code(dealer, candidate)
		if item:
			return item
	return None


_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]*")


def resolve_item_by_name(dealer: str, text: str) -> dict | None:
	"""Fallback for comms.flow_api.get_item_info, tried only once resolve_item_mention's
	exact private-dealer-code match has already failed. Scoped to
	dealer_catalog_api.catalog_for(dealer) -- the dealer's actual visible-and-sellable
	catalog -- not just items they have an Item Dealer Code for: those are two
	genuinely separate mechanisms (a Dealer Catalog assignment is "can this dealer see
	and order this item at all"; an Item Dealer Code is an optional private shorthand
	code layered on top), and a dealer routinely asks about a catalog-visible item
	they were never assigned a private code for at all. An item outside catalog_for is
	never matched into a reply, same visibility boundary resolve_dealer_code enforces
	for its own, narrower case.

	Two matching strategies, tried in order:
	1. Exact, case-insensitive substring containment against the item's own code
	   ("RUSTIC-GREY" inside "PT-4040-RUSTIC-GREY") -- a dealer shortens the real
	   item code at least as often as they type its name, and this needs no fuzzy
	   tolerance since it's already an exact match once case is ignored.
	2. A fuzzy match against the item's name, tolerant of a minor typo ("Royal Glass"
	   for "Royal Glassy") and of the name being wrapped inside a full sentence --
	   often in Hindi/Hinglish, with the item name itself still typed in Latin script
	   ("क्या आप ... Royal Glossy ... सकते हैं?"). Fuzzy-matching that whole sentence
	   against a two-word item name washes the match out with unrelated surrounding
	   text, so this extracts just the Latin-script words and fuzzy-matches short
	   windows of them (1-3 consecutive words -- the shape an item name actually
	   takes) rather than the raw text as one blob; the raw text is still tried too;
	   whichever window scores highest overall wins.

	Deliberately NOT used by the free-text LLM path (comms/api.py's _maybe_auto_reply):
	there, item_mention is an arbitrary phrase pulled out of a longer, unprompted
	sentence, where either strategy above risks confidently resolving to the wrong
	item. Here the dealer's entire reply is a single, deliberate answer to a single
	question, so a close match is a safe bet."""
	text = (text or "").strip()
	if not text:
		return None

	from dms_erp.catalog.dealer_catalog_api import catalog_for

	item_codes = catalog_for(dealer)
	if not item_codes:
		return None
	items = frappe.get_all("Item", filters={"name": ["in", item_codes]}, fields=["name", "item_name"])
	if not items:
		return None

	upper_text = text.upper()
	for item in items:
		if upper_text in item.name.upper():
			return _serialize(frappe.get_doc("Item", item.name))

	by_lower_name = {item.item_name.lower(): item.name for item in items if item.item_name}
	if not by_lower_name:
		return None

	words = _LATIN_WORD_RE.findall(text)
	candidates = {" ".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, min(i + 4, len(words) + 1))}
	candidates.add(text)  # covers a name that doesn't split cleanly into separate words

	best_name, best_ratio = None, 0.0
	for candidate in candidates:
		match = difflib.get_close_matches(candidate.lower(), by_lower_name.keys(), n=1, cutoff=0.6)
		if not match:
			continue
		ratio = difflib.SequenceMatcher(None, candidate.lower(), match[0]).ratio()
		if ratio > best_ratio:
			best_name, best_ratio = match[0], ratio

	if not best_name:
		return None
	return _serialize(frappe.get_doc("Item", by_lower_name[best_name]))


@frappe.whitelist(methods=["GET"])
def list_item_groups(search: str | None = None, limit: int = 20, offset: int = 0):
	# is_group: 0 excludes the root ("All Item Groups") -- catalog/setup.py seeds
	# every category as a leaf under that root, so this is the full, flat list of
	# categories a product form should offer, with no separate detail endpoint
	# needed (there's nothing more to a category than its name and parent).
	limit, offset = clamp(limit, offset)
	filters = {"is_group": 0}
	if search:
		filters["item_group_name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Item Group", filters=filters)
	rows = frappe.get_all(
		"Item Group",
		filters=filters,
		fields=["name", "item_group_name", "parent_item_group"],
		order_by="item_group_name asc",
		limit_start=offset,
		limit_page_length=limit,
	)
	return {
		"items": [
			{"id": r.name, "name": r.item_group_name, "parentItemGroup": r.parent_item_group} for r in rows
		],
		"total": total,
		"limit": limit,
		"offset": offset,
	}
