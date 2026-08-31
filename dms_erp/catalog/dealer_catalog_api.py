"""Dealer-specific catalog visibility (BRD §6.4 — "required from go-live").

No ERPNext doctype models per-customer item visibility, so Dealer Catalog is a
genuine custom doctype: one row per dealer (Customer), holding a child table of the
items they're allowed to see. A dealer with no Dealer Catalog record yet falls back
to the FULL catalog (unfiltered, matching pacific-tileflow's catalogFor()) so an
unassigned dealer isn't silently blocked from everything.

Per the BRD flow, Purchase (and Management) confirm/maintain catalog assignments;
Sales only needs read access (the /inquiries item picker filters against it).

`is_visible` stays a pure per-dealer assignment check — whether Purchase has opted an
item into this dealer's catalog, independent of the item's own lifecycle (the Dealer
Catalog editor still needs to show/toggle a Pulled Back item that's currently
assigned, so an admin can remove it). `catalog_for` is different: per its own
docstring it's "what a dealer is allowed to inquire/quote for", and a Pulled Back
item can't be quoted for *any* dealer regardless of assignment — so it's filtered by
`catalog.utils.is_sellable` on top of the assignment/fallback logic (Phase 11; this
was previously visibility-only, letting a discontinued item stay in an "effective
catalog" it should never have appeared in).
"""

import frappe
from frappe import _

from dms_erp.catalog.utils import is_sellable

CATALOG_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}


def _sellable_item_codes(item_codes: list[str]) -> list[str]:
	if not item_codes:
		return []
	rows = frappe.get_all("Item", filters={"name": ["in", item_codes]}, fields=["name", "custom_discontinuation_status"])
	return [r.name for r in rows if is_sellable(r.custom_discontinuation_status or "Active")]


def _assert_can_manage_catalog():
	if not set(frappe.get_roles(frappe.session.user)) & CATALOG_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage dealer catalogs."), frappe.PermissionError)


def _get_or_create(dealer: str) -> "frappe.model.document.Document":
	if frappe.db.exists("Dealer Catalog", dealer):
		return frappe.get_doc("Dealer Catalog", dealer)
	doc = frappe.get_doc({"doctype": "Dealer Catalog", "dealer": dealer, "items": []})
	doc.insert(ignore_permissions=True)
	return doc


@frappe.whitelist(methods=["GET"])
def is_visible(dealer: str, item: str) -> bool:
	if not frappe.db.exists("Dealer Catalog", dealer):
		return True
	return frappe.db.exists("Dealer Catalog Item", {"parent": dealer, "item": item}) is not None


@frappe.whitelist(methods=["GET"])
def catalog_for(dealer: str):
	"""Item codes a dealer is allowed to inquire/quote for — assignment (or the
	unassigned-dealer fallback to everything) narrowed to currently-sellable items."""
	if not frappe.db.exists("Dealer Catalog", dealer):
		return _sellable_item_codes(frappe.get_all("Item", pluck="name"))
	assigned = frappe.get_all("Dealer Catalog Item", filters={"parent": dealer}, pluck="item")
	return _sellable_item_codes(assigned)


@frappe.whitelist(methods=["POST", "PUT"])
def set_product_visibility(dealer: str, item: str, visible: bool):
	_assert_can_manage_catalog()

	doc = _get_or_create(dealer)
	already_visible = any(row.item == item for row in doc.items)

	if visible and not already_visible:
		doc.append("items", {"item": item})
		doc.save(ignore_permissions=True)
	elif not visible and already_visible:
		doc.items = [row for row in doc.items if row.item != item]
		doc.save(ignore_permissions=True)

	return {"success": True}


@frappe.whitelist(methods=["POST", "PUT"])
def set_category_visibility(dealer: str, item_group: str, visible: bool):
	_assert_can_manage_catalog()

	doc = _get_or_create(dealer)
	category_items = set(frappe.get_all("Item", filters={"item_group": item_group}, pluck="name"))
	current = {row.item for row in doc.items}

	updated = (current | category_items) if visible else (current - category_items)
	doc.items = [{"item": item} for item in updated]
	doc.save(ignore_permissions=True)

	return {"success": True}


@frappe.whitelist(methods=["GET"])
def category_coverage(dealer: str, item_group: str):
	total = frappe.db.count("Item", {"item_group": item_group})
	if not frappe.db.exists("Dealer Catalog", dealer):
		return {"total": total, "visible": total}

	visible = frappe.db.count(
		"Dealer Catalog Item",
		{"parent": dealer, "item": ["in", frappe.get_all("Item", filters={"item_group": item_group}, pluck="name")]},
	)
	return {"total": total, "visible": visible}
