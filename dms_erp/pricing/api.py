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
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from dms_erp.pagination import clamp
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


def get_dealer_price(item: str) -> float | None:
	return frappe.db.get_value("Item Price", {"item_code": item, "price_list": DEALER_PRICE_LIST}, "price_list_rate")


def set_dealer_price(item: str, rate: float):
	name = frappe.db.get_value("Item Price", {"item_code": item, "price_list": DEALER_PRICE_LIST}, "name")
	if name:
		frappe.db.set_value("Item Price", name, "price_list_rate", rate)
		return
	frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item,
			"price_list": DEALER_PRICE_LIST,
			"selling": 1,
			"price_list_rate": rate,
		}
	).insert(ignore_permissions=True)


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
def list_price_records(limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	total = frappe.db.count("Item Price Proposal")
	names = frappe.get_all("Item Price Proposal", pluck="name", limit_start=offset, limit_page_length=limit)
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

	return _serialize(doc)
