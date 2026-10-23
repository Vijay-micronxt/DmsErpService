"""Product launch pricing (BRD §7.4).

Item Price Proposal is a custom doctype because ERPNext has no native equivalent for
"a proposed landing-cost/margin breakdown awaiting approval, with an audit trail" —
but the *live* price it eventually publishes is the standard ERPNext Item Price (on
the "Dealer" selling price list), not a custom field, so every other part of ERPNext
that reads item pricing sees the normal thing.

Only Purchase/Management (or System Manager) can create proposals or approve prices —
Sales/Warehouse can read them. `approved_by` is always the authenticated caller
(frappe.session.user), never a client-supplied value, now that Phase 0 gives us a
real identity to trust.

BRD C.1.4/C.7.1 dealer price-tier classification: approve_price still always
publishes the approved price onto the "Dealer" list (unchanged). If the item has a
Series (catalog.api create_product's series_ref), any Standard Dealer/Master Dealer
rates that Series carries in its price_list_rates are published alongside it in the
same call — an item with no Series, or a Series with no rates for those two lists,
simply doesn't get them yet, same as before this existed.

get_dealer_tier_prices/set_dealer_tier_price are the whitelisted read/write pair for
a price-list management screen — get_price_for_dealer/set_price_for_list already did
the underlying work (used by quotation_api/order_api's rate lookup and
_publish_series_tier_rates) but neither was itself callable from the frontend, so
there was no way to view or hand-set a tier rate for an item whose Series doesn't
carry pre-configured price_list_rates.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, nowdate

from dms_erp.pagination import clamp
from dms_erp.pricing.dealer_classification import DEALER_CLASSIFICATION_MASTER, DEALER_CLASSIFICATION_STANDARD
from dms_erp.pricing.setup import DEALER_PRICE_LIST, PRICE_LISTS

PRICING_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}


def _assert_can_manage_pricing():
	if not set(frappe.get_roles(frappe.session.user)) & PRICING_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage pricing."), frappe.PermissionError)


def _serialize(doc: "frappe.model.document.Document") -> dict:
	return {
		"productId": doc.item,
		"supplier": doc.supplier,
		"purchaseCost": doc.purchase_cost,
		"freight": doc.freight,
		"handling": doc.handling,
		"otherCosts": doc.other_costs,
		"marginPct": doc.margin_pct,
		"effectiveDate": doc.effective_date,
		"status": doc.status,
		"remarks": doc.remarks,
		"landingCost": doc.landing_cost(),
		"suggestedPrice": doc.suggested_price(),
		# Newest first, matching how the frontend prepends new entries to its history array.
		"history": [
			{
				"id": row.name,
				"oldPrice": row.old_price,
				"newPrice": row.new_price,
				"costPrice": row.cost_price,
				"marginPct": row.margin_pct,
				"effectiveDate": row.effective_date,
				"approvedBy": row.approved_by,
				"reason": row.reason,
				"updatedAt": row.updated_at,
			}
			for row in sorted(doc.history, key=lambda r: r.idx, reverse=True)
		],
	}


def get_dealer_price(item: str, price_list: str = DEALER_PRICE_LIST) -> float | None:
	return frappe.db.get_value("Item Price", {"item_code": item, "price_list": price_list}, "price_list_rate")


def get_price_for_dealer(item: str, dealer: str) -> float | None:
	"""BRD C.1.4/C.7.1: rate keyed by the dealer's current classification tier
	(quotation_api's rate lookup) rather than always the flat "Dealer" list. Falls
	back to the plain "Dealer" price when the dealer's own tier has no published
	rate yet for this item — the common case until a Series with tiered
	price_list_rates exists for it — so this is a drop-in, backward-compatible
	replacement for get_dealer_price(item) everywhere it was called with a dealer
	already in hand.

	Customer.default_price_list (ERPNext's own native field, otherwise unused in
	this app) is a manual override that always wins over the classification tier
	when staff have set it — a dealer with a genuine business reason to be billed
	off a specific list (export, a project rate, whatever other Price Lists exist
	on the site) isn't just another tier alongside Standard/Dealer/Master, it
	fully replaces the classification lookup for that dealer. Still falls back to
	the flat "Dealer" list if the tagged list has no rate for this item yet, same
	safety net the classification path already has — a manual tag should never
	produce a hard "no price" where the untagged path would have found one."""
	tagged_price_list = frappe.db.get_value("Customer", dealer, "default_price_list")
	if tagged_price_list:
		rate = get_dealer_price(item, price_list=tagged_price_list)
		if rate is not None:
			return rate
		return get_dealer_price(item)

	classification = frappe.db.get_value("Customer", dealer, "custom_dealer_classification") or DEALER_CLASSIFICATION_STANDARD
	rate = get_dealer_price(item, price_list=classification)
	if rate is not None:
		return rate
	return get_dealer_price(item)


def set_price_for_list(item: str, price_list: str, rate: float):
	name = frappe.db.get_value("Item Price", {"item_code": item, "price_list": price_list}, "name")
	if name:
		frappe.db.set_value("Item Price", name, "price_list_rate", rate)
		return
	frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item,
			"price_list": price_list,
			"selling": 1,
			"price_list_rate": rate,
		}
	).insert(ignore_permissions=True)


def set_dealer_price(item: str, rate: float):
	set_price_for_list(item, DEALER_PRICE_LIST, rate)


@frappe.whitelist(methods=["GET"])
def get_dealer_tier_prices(item: str) -> dict:
	"""BRD C.1.4/C.7.1 — the three tier price-list rates for an item, for a price-list
	management screen. None for a list that has no rate published yet."""
	return {price_list: get_dealer_price(item, price_list=price_list) for price_list in PRICE_LISTS}


@frappe.whitelist(methods=["POST", "PUT"])
def set_dealer_tier_price(item: str, price_list: str, rate: float):
	_assert_can_manage_pricing()
	if price_list not in PRICE_LISTS:
		frappe.throw(_("Invalid price_list: {0}").format(price_list), frappe.ValidationError)
	set_price_for_list(item, price_list, rate)
	return get_dealer_tier_prices(item)


def _publish_series_tier_rates(item: str):
	series_ref = frappe.db.get_value("Item", item, "custom_series_ref")
	if not series_ref:
		return
	other_tiers = [DEALER_CLASSIFICATION_STANDARD, DEALER_CLASSIFICATION_MASTER]
	rows = frappe.get_all(
		"Series Price List Rate", filters={"parent": series_ref, "price_list": ["in", other_tiers]}, fields=["price_list", "rate"]
	)
	for row in rows:
		set_price_for_list(item, row.price_list, row.rate)


def ensure_price_record(item: str, supplier: str, purchase_cost: float, margin_pct: float, effective_date, remarks: str | None = None):
	"""Called internally from the catalog module's create_product — not a standalone
	user action, so it isn't itself whitelisted (matches how the frontend only ever
	calls ensurePriceRecord() from the Product Master's Add Item flow)."""
	if frappe.db.exists("Item Price Proposal", item):
		return frappe.get_doc("Item Price Proposal", item)

	doc = frappe.get_doc(
		{
			"doctype": "Item Price Proposal",
			"item": item,
			"supplier": supplier,
			"purchase_cost": purchase_cost,
			"margin_pct": margin_pct,
			"effective_date": effective_date,
			"status": "Pending",
			"remarks": remarks or "New item launch — confirm landing cost and margin before publishing.",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def list_all_price_records() -> list[dict]:
	"""Unpaginated — for internal callers (reports) that need the full result set,
	not a page of it. list_price_records (the whitelisted endpoint) is the paginated one."""
	names = frappe.get_all("Item Price Proposal", pluck="name")
	return [_serialize(frappe.get_doc("Item Price Proposal", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_price_records(search: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = {"name": ["like", f"%{search}%"]} if search else {}
	total = frappe.db.count("Item Price Proposal", filters=filters)
	names = frappe.get_all("Item Price Proposal", filters=filters, pluck="name", limit_start=offset, limit_page_length=limit)
	return {
		"items": [_serialize(frappe.get_doc("Item Price Proposal", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_price_record(item: str):
	if not frappe.db.exists("Item Price Proposal", item):
		return None
	return _serialize(frappe.get_doc("Item Price Proposal", item))


# ISS-2026-00623's own blocker: there's no real master purchase price list or
# per-tier margin % yet (Pacific hasn't supplied one — MOM action items #14/#15).
# This doesn't wait on that: Purchase Invoice and Sales Invoice are both
# migration-only data in this app (it posts neither itself, live — see
# purchase/setup.py's own note and dashboard.api's "no real Sales Invoice/AR
# ledger" docstring), so every line in them is real, historical Tally pricing --
# exactly the "previous data" a landing-cost/margin proposal should be able to
# start from instead of a blank form.
_PRICE_HISTORY_MIN_SAMPLE = 2


def _median(values: list[float]) -> float:
	ordered = sorted(values)
	n = len(ordered)
	mid = n // 2
	return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


@frappe.whitelist(methods=["GET"])
def suggest_price_from_history(item: str) -> dict | None:
	"""A purchase-cost + margin % SUGGESTION for Item Price Proposal, derived from
	this item's own historical Purchase Invoice (cost) and Sales Invoice (selling
	price) lines -- never auto-applied, same "take it or leave it" convention as
	sales.quotation_api.suggest_transport_rate. Median, not mean: a live check
	against the real migrated data (2026-10-13) showed real outliers in both
	directions (a stray non-tile ledger line, a couple of discontinued items sold
	below cost during clearance) that would otherwise skew an average into
	nonsense; a handful of outliers barely move a median.

	Deliberately NOT tier-specific (Standard/Dealer/Master) despite ISS-2026-00623
	asking for a per-tier margin: that same live check found every single migrated
	Customer classified "Standard Dealer" -- the nightly reclassification job has
	never actually differentiated a dealer into "Dealer"/"Master Dealer" yet, so
	there is no tier-tagged sales history to derive three different margins from.
	This returns one blended margin from all historical sales of the item,
	regardless of which dealer bought it; building the real per-tier version is a
	follow-up once dealer classification actually diverges.

	Returns None (not a zero/guessed number) when either side has fewer than
	_PRICE_HISTORY_MIN_SAMPLE real data points -- a brand-new item with no Tally
	history, or only one data point, gets no suggestion rather than a number
	nobody should trust."""
	purchase_rates = frappe.get_all(
		"Purchase Invoice Item", filters={"item_code": item, "rate": [">", 0], "docstatus": 1}, pluck="rate"
	)
	sale_rates = frappe.get_all(
		"Sales Invoice Item", filters={"item_code": item, "rate": [">", 0], "docstatus": 1}, pluck="rate"
	)

	if len(purchase_rates) < _PRICE_HISTORY_MIN_SAMPLE or len(sale_rates) < _PRICE_HISTORY_MIN_SAMPLE:
		return None

	suggested_purchase_cost = _median(purchase_rates)
	suggested_sale_rate = _median(sale_rates)
	if suggested_purchase_cost <= 0:
		return None

	return {
		"suggestedPurchaseCost": round(suggested_purchase_cost, 2),
		"suggestedMarginPct": round((suggested_sale_rate - suggested_purchase_cost) / suggested_purchase_cost * 100, 1),
		"purchaseSampleSize": len(purchase_rates),
		"saleSampleSize": len(sale_rates),
	}


@frappe.whitelist(methods=["POST", "PUT"])
def save_cost_inputs(
	item: str,
	supplier: str,
	purchase_cost: float,
	freight: float = 0,
	handling: float = 0,
	other_costs: float = 0,
	margin_pct: float = 0,
	effective_date=None,
	remarks: str | None = None,
):
	_assert_can_manage_pricing()

	# Upsert, not update-only: every Tally-migrated item (all of them, on a migrated
	# site) has live Item Prices but no proposal, because proposals were only ever
	# seeded by create_product. Without this the pricing screen had no way to start
	# a landing-cost/margin record for any existing item.
	if not frappe.db.exists("Item Price Proposal", item):
		ensure_price_record(item, supplier, purchase_cost, margin_pct, effective_date or nowdate(), remarks)
	doc = frappe.get_doc("Item Price Proposal", item)
	doc.supplier = supplier
	doc.purchase_cost = purchase_cost
	doc.freight = freight
	doc.handling = handling
	doc.other_costs = other_costs
	doc.margin_pct = margin_pct
	if effective_date:
		doc.effective_date = effective_date
	doc.remarks = remarks or doc.remarks
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def approve_price(item: str, final_price: float, reason: str | None = None):
	_assert_can_manage_pricing()

	doc = frappe.get_doc("Item Price Proposal", item)
	old_price = get_dealer_price(item)

	doc.append(
		"history",
		{
			"old_price": old_price,
			"new_price": final_price,
			"cost_price": doc.landing_cost(),
			"margin_pct": doc.margin_pct,
			"effective_date": doc.effective_date,
			"approved_by": frappe.session.user,
			"reason": (reason or "").strip() or "Price approved",
			"updated_at": now_datetime(),
		},
	)
	doc.status = "Approved"
	doc.save(ignore_permissions=True)

	# Approved price becomes the live catalog price everywhere Item Price is read.
	set_dealer_price(item, final_price)
	_publish_series_tier_rates(item)

	return _serialize(doc)
