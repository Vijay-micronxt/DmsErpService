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
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from dms_erp.pagination import clamp
from dms_erp.pricing.dealer_classification import DEALER_CLASSIFICATION_MASTER, DEALER_CLASSIFICATION_STANDARD
from dms_erp.pricing.setup import DEALER_PRICE_LIST

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
	already in hand."""
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
