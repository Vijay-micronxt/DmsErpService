"""Dealer-specific catalog visibility (BRD §6.4 — "required from go-live").

No ERPNext doctype models per-customer item visibility, so Dealer Catalog is a
genuine custom doctype: one row per dealer (Customer), holding a child table of the
items they're allowed to see. A dealer with no Dealer Catalog record yet falls back
to the FULL catalog (unfiltered, matching pacific-tileflow's catalogFor()) so an
unassigned dealer isn't silently blocked from everything.

Per the BRD flow, Purchase (and Management) confirm/maintain catalog assignments;
Sales only needs read access (the /inquiries item picker filters against it).
"""

import frappe
from frappe import _

CATALOG_WRITE_ROLES = {"Pacific Purchase", "Pacific Management", "System Manager"}


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
	"""Item codes a dealer is allowed to inquire/quote for."""
	if not frappe.db.exists("Dealer Catalog", dealer):
		return frappe.get_all("Item", pluck="name")
	return frappe.get_all("Dealer Catalog Item", filters={"parent": dealer}, pluck="item")


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
