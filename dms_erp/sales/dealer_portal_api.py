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

from dms_erp.catalog.api import get_product, resolve_dealer_code
from dms_erp.catalog.dealer_catalog_api import catalog_for, is_visible
from dms_erp.catalog.utils import is_sellable
from dms_erp.pagination import clamp
from dms_erp.pricing.api import get_price_for_dealer
from dms_erp.sales.inquiry_api import _create_inquiry, _inquiry_filters, _serialize as _serialize_inquiry
from dms_erp.sales.order_api import _create_order, _serialize as _serialize_order
from dms_erp.warehouse.utils import top_batches, total_stock_for_item


def _current_dealer() -> str:
	dealer = frappe.db.get_value("User", frappe.session.user, "custom_dealer")
	if not dealer:
		frappe.throw(_("This account is not linked to a dealer."), frappe.PermissionError)
	return dealer


def _catalog_entry(item_code: str, dealer: str) -> dict:
	"""A trimmed, dealer-safe view of catalog.api.get_product's full serialization --
	drops dealerCodes (every dealer's own item code for this item, including other
	dealers') down to just this dealer's own code, and adds the top-3 on-hand
	batches BRD C.13.1 wants shown at item-detail time."""
	product = get_product(item_code)
	own_code = next((row["customerItemCode"] for row in product["dealerCodes"] if row["dealer"] == dealer), None)
	del product["dealerCodes"]
	product["dealerCode"] = own_code
	product["price"] = get_price_for_dealer(item_code, dealer)
	product["topBatches"] = top_batches(item_code)
	return product


@frappe.whitelist(methods=["GET"])
def get_catalog(search: str | None = None, category: str | None = None, limit: int = 20, offset: int = 0):
	"""This dealer's own assigned-and-sellable catalog (BRD C.1.5/C.13.1) — search by
	item name only; resolve_code is the separate lookup for a dealer's own code or the
	company item code."""
	dealer = _current_dealer()
	limit, offset = clamp(limit, offset)

	item_codes = set(catalog_for(dealer))
	filters = {"name": ["in", list(item_codes) or [""]]}
	if search:
		filters["item_name"] = ["like", f"%{search}%"]
	if category:
		filters["item_group"] = category

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


@frappe.whitelist(methods=["GET"])
def resolve_code(code: str) -> dict | None:
	"""BRD C.13.1 — search by the dealer's own code OR the company item code, either
	one. resolve_dealer_code only ever resolved the dealer's own mapped code; this
	adds the company-code fallback (must still be visible/sellable for this dealer)
	so both entry points actually work from the portal, not just one."""
	dealer = _current_dealer()

	product = resolve_dealer_code(dealer, code)
	if not product:
		if frappe.db.exists("Item", code) and is_visible(dealer, code):
			status = frappe.get_cached_value("Item", code, "custom_discontinuation_status") or "Active"
			if is_sellable(status):
				product = get_product(code)

	if not product:
		return None
	return _catalog_entry(product["id"], dealer)


@frappe.whitelist(methods=["GET"])
def get_item(item: str) -> dict:
	dealer = _current_dealer()
	if not is_visible(dealer, item):
		frappe.throw(_("{0} is not in your catalog.").format(item), frappe.PermissionError)
	return _catalog_entry(item, dealer)


@frappe.whitelist(methods=["POST"])
def raise_inquiry(item: str, qty: float, remarks: str | None = None):
	"""BRD C.13.1 — the out-of-stock / "raise an enquiry" path. Always source="Web"
	and always unassigned (see inquiry_api._create_inquiry's own staff-only
	self-assignment default) since this is the dealer, not staff, raising it."""
	dealer = _current_dealer()
	return _create_inquiry(dealer, item, qty, source="Web", remarks=remarks)


@frappe.whitelist(methods=["GET"])
def suggest_alternatives(item: str, limit: int = 5) -> list[dict]:
	"""BRD C.13.1 — "see alternative/recommended items" when the item raised in an
	enquiry is out of stock. Same-series items in this dealer's own catalog, ranked
	by on-hand stock, excluding the out-of-stock item itself -- the closest
	approximation to "recommended" available without a dedicated recommendation
	engine (none exists anywhere else in this app either)."""
	dealer = _current_dealer()
	limit, _offset = clamp(limit, 0)

	series_ref = frappe.get_cached_value("Item", item, "custom_series_ref")
	item_codes = set(catalog_for(dealer)) - {item}
	if series_ref:
		same_series = set(frappe.get_all("Item", filters={"custom_series_ref": series_ref}, pluck="name"))
		candidates = item_codes & same_series
	else:
		candidates = item_codes

	ranked = sorted(candidates, key=lambda code: total_stock_for_item(code), reverse=True)
	return [_catalog_entry(code, dealer) for code in ranked[:limit]]


@frappe.whitelist(methods=["POST"])
def convert_to_order(inquiry: str, expected_dispatch, customer_po: str):
	"""BRD C.13.1 — "enter their own PO number to tag/close it." Only converts an
	inquiry this dealer actually owns, at the same in-stock/priced items it was
	raised for -- lines come from the inquiry itself, not caller-supplied, so a
	dealer session can't order arbitrary items through this endpoint."""
	dealer = _current_dealer()
	if not customer_po:
		frappe.throw(_("customer_po is required."), frappe.ValidationError)

	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.dealer != dealer:
		frappe.throw(_("Inquiry {0} does not belong to you.").format(inquiry), frappe.PermissionError)
	if doc.status in {"Converted to Order", "Rejected", "Closed"}:
		frappe.throw(_("Inquiry {0} is {1} and can no longer be converted.").format(inquiry, doc.status), frappe.ValidationError)

	lines = [{"item": doc.item, "qty": doc.qty}]
	return _create_order(dealer, lines, expected_dispatch, inquiry, customer_po=customer_po)


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
		"items": [_serialize_inquiry(frappe.get_doc("Inquiry", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_my_inquiry(inquiry: str) -> dict:
	dealer = _current_dealer()
	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.dealer != dealer:
		frappe.throw(_("Inquiry {0} does not belong to you.").format(inquiry), frappe.PermissionError)
	return _serialize_inquiry(doc)


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


@frappe.whitelist(methods=["GET"])
def my_dues() -> dict:
	"""BRD C.13.1 — outstanding dues. Sales Invoice.outstanding_amount is real,
	correct SQL against the native ERPNext billing doctype; it simply returns 0
	today because nothing in this app raises a Sales Invoice yet anywhere. Not a
	stub -- a true, forward-compatible answer that starts returning real numbers
	the moment invoicing exists."""
	dealer = _current_dealer()
	total = frappe.db.sql(
		"select sum(outstanding_amount) from `tabSales Invoice` where customer=%s and docstatus=1",
		(dealer,),
	)[0][0]
	return {"outstanding": float(total or 0)}


@frappe.whitelist(methods=["GET"])
def my_profile() -> dict:
	dealer = _current_dealer()
	doc = frappe.get_doc("Customer", dealer)
	return {
		"id": doc.name,
		"name": doc.customer_name,
		"phone": doc.custom_phone,
		"classification": doc.custom_dealer_classification,
	}
