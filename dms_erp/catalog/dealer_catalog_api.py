"""Dealer-specific catalog visibility (BRD §6.4 — "required from go-live").

No ERPNext doctype models per-customer item visibility, so Dealer Catalog is a
genuine custom doctype: one row per dealer (Customer), holding a child table of the
items they're allowed to see. A dealer with no Dealer Catalog record yet falls back
to the FULL catalog (unfiltered, matching pacific-tileflow's catalogFor()) so an
unassigned dealer isn't silently blocked from everything.

Per the BRD flow, Purchase (and Management) confirm/maintain catalog assignments;
Sales only needs read access (the /inquiries item picker filters against it).

BRD C.1.5: "a dealer can only access items for which a sample has been issued to
that dealer... driven by the dealer's issued-sample list, not a manual assignment."
catalog.sample_api.issue_sample is now that path -- it calls _set_product_visibility
directly the moment a sample is actually issued, so day-to-day visibility follows
sample issuance rather than a separate manual step. set_product_visibility /
set_category_visibility stay whitelisted as Purchase/Management admin overrides
(bulk category grants, manual correction) -- the assignment mechanism itself didn't
change, only what normally drives it.

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

from html import escape as _esc

import frappe
from frappe import _
from frappe.utils import now_datetime

from dms_erp.catalog.utils import is_sellable
from dms_erp.pricing.api import get_price_for_dealer

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


def _set_product_visibility(dealer: str, item: str, visible: bool) -> dict:
	"""Unguarded core of set_product_visibility -- also called directly by
	catalog.sample_api.issue_sample, whose own DMS Warehouse/Management role check is
	the actual authorization for that path (see this module's docstring)."""
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
def set_product_visibility(dealer: str, item: str, visible: bool):
	_assert_can_manage_catalog()
	return _set_product_visibility(dealer, item, visible)


@frappe.whitelist(methods=["POST", "PUT"])
def set_category_visibility(dealer: str, item_group: str, visible: bool):
	_assert_can_manage_catalog()

	doc = _get_or_create(dealer)
	category_items = set(frappe.get_all("Item", filters={"item_group": item_group}, pluck="name"))
	current = {row.item for row in doc.items}

	updated = (current | category_items) if visible else (current - category_items)
	doc.set("items", [{"item": item} for item in updated])
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


_CATALOG_EXPORT_CSS = """
	body { margin: 0; padding: 24px; font-family: -apple-system, Helvetica, Arial, sans-serif; color: #1b2024; }
	h1 { font-size: 18px; margin: 0 0 2px; }
	.meta { font-size: 12px; color: #545c61; margin: 0 0 20px; }
	table { width: 100%; border-collapse: collapse; font-size: 12px; }
	th, td { border-bottom: 1px solid #dce0df; padding: 8px 10px; text-align: left; vertical-align: middle; }
	th { font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em; color: #545c61; }
	td.price, th.price { text-align: right; white-space: nowrap; }
	img.thumb { width: 40px; height: 40px; object-fit: cover; border-radius: 4px; background: #f5f6f5; }
	@media print { body { padding: 0; } }
"""


@frappe.whitelist(methods=["GET"])
def dealer_catalog_export(dealer: str, include_price: str | int | bool = True) -> str:
	"""BRD C.14 — a per-dealer catalog sheet limited to what catalog_for(dealer)
	already resolves as visible-and-sellable for that dealer, rendered as printable
	HTML the same way allocation_api.render_box_stickers_html renders sticker sheets
	(plain HTML in the ordinary JSON envelope, not a Desk Print Format — this
	API-only app never redirects into /app). `include_price` is the BRD's
	price-inclusive/exclusive format toggle; the price shown, when included, is this
	dealer's own tier rate (pricing.get_price_for_dealer) rather than a flat list
	price, so a Master Dealer's export doesn't leak a Standard Dealer's number or
	vice versa."""
	include_price_flag = str(include_price).strip().lower() in {"1", "true", "yes"}
	item_codes = catalog_for(dealer)
	dealer_name = frappe.db.get_value("Customer", dealer, "customer_name") or dealer

	items = (
		frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "item_name", "custom_size", "custom_finish", "custom_series", "item_group"],
			order_by="item_group asc, item_name asc",
		)
		if item_codes
		else []
	)

	dealer_codes = (
		{
			row.parent: row.customer_item_code
			for row in frappe.get_all(
				"Item Dealer Code", filters={"parent": ["in", item_codes], "dealer": dealer}, fields=["parent", "customer_item_code"]
			)
		}
		if item_codes
		else {}
	)
	primary_images = (
		{
			row.parent: row.image
			for row in frappe.get_all(
				"Product Image", filters={"parent": ["in", item_codes], "is_primary": 1}, fields=["parent", "image"]
			)
		}
		if item_codes
		else {}
	)

	price_header = "<th class=\"price\">Price / box</th>" if include_price_flag else ""
	rows = []
	for it in items:
		price_cell = ""
		if include_price_flag:
			rate = get_price_for_dealer(it.name, dealer)
			price_cell = f"<td class=\"price\">{f'₹{rate:,.2f}' if rate is not None else '—'}</td>"
		image = primary_images.get(it.name)
		thumb = f"<img class=\"thumb\" src=\"{_esc(image)}\" alt=\"\">" if image else ""
		dealer_code = dealer_codes.get(it.name)
		code_display = f"{_esc(it.name)}<br><span style=\"color:#8a9196\">{_esc(dealer_code)}</span>" if dealer_code else _esc(it.name)
		rows.append(
			f"""<tr>
	<td>{thumb}</td>
	<td>{code_display}</td>
	<td>{_esc(it.item_name or '')}</td>
	<td>{_esc(it.custom_series or '—')}</td>
	<td>{_esc(it.custom_size or '—')} / {_esc(it.custom_finish or '—')}</td>
	{price_cell}
</tr>"""
		)

	table = f"""<table>
	<thead><tr><th></th><th>Item code</th><th>Name</th><th>Series</th><th>Size / Finish</th>{price_header}</tr></thead>
	<tbody>{''.join(rows) if rows else '<tr><td colspan="6">No items are currently visible in this dealer’s catalog.</td></tr>'}</tbody>
</table>"""

	return (
		f"<!doctype html><html><head><meta charset='utf-8'><title>Catalog — {_esc(dealer_name)}</title>"
		f"<style>{_CATALOG_EXPORT_CSS}</style></head><body>"
		f"<h1>Pacific Inc — Dealer Catalog</h1>"
		f"<p class=\"meta\">{_esc(dealer_name)} &middot; {len(items)} items"
		f"{' &middot; price-inclusive format' if include_price_flag else ' &middot; price-exclusive format'}"
		f" &middot; generated {now_datetime().strftime('%d %b %Y')}</p>"
		f"{table}</body></html>"
	)
