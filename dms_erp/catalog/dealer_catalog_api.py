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
from frappe.utils import now_datetime, sbool

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
	"""Item codes a dealer is allowed to order/quote for — assignment (or the
	unassigned-dealer fallback to everything) narrowed to currently-sellable items.
	This is the ORDERABLE set; see browsable_catalog_for for the wider, view-only one."""
	if not frappe.db.exists("Dealer Catalog", dealer):
		return _sellable_item_codes(frappe.get_all("Item", pluck="name"))
	assigned = frappe.get_all("Dealer Catalog Item", filters={"parent": dealer}, pluck="item")
	return _sellable_item_codes(assigned)


@frappe.whitelist(methods=["GET"])
def list_catalog_assignments() -> dict:
	"""{dealer: [assigned item codes]} for every dealer that HAS a Dealer Catalog --
	a dealer absent from the map has none and sees the full (sellable) catalog. The
	staff app used to call catalog_for once per dealer, each returning the whole
	catalog for unassigned dealers; once the app held all ~4,500 items that was
	dealers x 4,500 codes over the wire. Raw assignment, not sellable-filtered: the
	frontend already applies isSellable itself everywhere it builds an orderable list."""
	out = {}
	for row in frappe.get_all("Dealer Catalog Item", fields=["parent", "item"], order_by="parent asc, idx asc"):
		out.setdefault(row.parent, []).append(row.item)
	for dealer in frappe.get_all("Dealer Catalog", pluck="name"):
		out.setdefault(dealer, [])
	return out


@frappe.whitelist(methods=["GET"])
def browsable_catalog_for(dealer: str):
	"""ISS-2026-00644 — "dealers can see unassigned items too, but can only inquire,
	not order, on them." Every currently-sellable item in the system, regardless of
	this dealer's own Dealer Catalog assignment: a dealer may always see/inquire on
	any sellable item, but catalog_for's narrower assigned set is still what's
	actually orderable (dealer_portal_api.convert_to_order and comms.flow_api's
	WhatsApp order-placement path both still gate on catalog_for/is_visible)."""
	return _sellable_item_codes(frappe.get_all("Item", pluck="name"))


def _materialize_full_catalog(dealer: str) -> "frappe.model.document.Document":
	"""A first Dealer Catalog for a dealer who has none yet, seeded with every item --
	exactly what they could already see via the no-record fallback -- so an admin's
	first *hide* narrows that set instead of replacing it. Creating it empty (what
	_get_or_create does) and then removing one item left the dealer able to order
	nothing at all: a real report, hiding one item from a dealer with no catalog took
	their orderable set from 4,338 items to 0. Every Item, not only sellable ones,
	since is_visible's own fallback is "everything" and catalog_for re-applies the
	sellable filter on read anyway."""
	doc = frappe.get_doc(
		{"doctype": "Dealer Catalog", "dealer": dealer, "items": [{"item": code} for code in frappe.get_all("Item", pluck="name")]}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _set_product_visibility(dealer: str, item: str, visible: bool) -> dict:
	"""Unguarded core of set_product_visibility -- also called directly by
	catalog.sample_api.issue_sample, whose own DMS Warehouse/Management role check is
	the actual authorization for that path (see this module's docstring).

	Hiding an item from a dealer with no catalog yet starts from the full set they
	could already see (_materialize_full_catalog), never an empty one. Showing one
	still creates a catalog of just that item, as before -- that's BRD C.1.5's
	sample-driven catalog starting with its first issued sample."""
	if not visible and not frappe.db.exists("Dealer Catalog", dealer):
		doc = _materialize_full_catalog(dealer)
	else:
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
	return _set_product_visibility(dealer, item, sbool(visible))


@frappe.whitelist(methods=["POST", "PUT"])
def set_category_visibility(dealer: str, item_group: str, visible: bool):
	_assert_can_manage_catalog()
	visible = sbool(visible)

	# Same trap as _set_product_visibility: "Hide all" on a dealer with no catalog yet
	# must start from the full set they could already see, not an empty one.
	if not visible and not frappe.db.exists("Dealer Catalog", dealer):
		doc = _materialize_full_catalog(dealer)
	else:
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
	td.imgs { width: 1%; }
	.imgs-row { display: flex; flex-wrap: wrap; gap: 6px; width: 260px; }
	figure { margin: 0; display: grid; gap: 2px; justify-items: center; }
	figure img { width: 48px; height: 48px; object-fit: cover; border-radius: 4px; background: #f5f6f5; display: block; }
	figure.main img { width: 72px; height: 72px; }
	figcaption { font-size: 9px; color: #8a9196; text-transform: uppercase; letter-spacing: .04em; }
	.more { align-self: center; font-size: 10px; color: #8a9196; }
	@media print { body { padding: 0; } }
"""


# Shown under each thumbnail so a dealer can tell the room shot from the product shot.
_IMAGE_CAPTIONS = {"Product": "Product", "Application": "Room", "Additional": "More"}
_MAX_EXPORT_IMAGES = 6


def _absolute_file_url(path: str) -> str:
	"""Image paths are stored site-relative (/files/...). The export is opened from the
	staff app, which may live on another host, so a relative path would point there."""
	return frappe.utils.get_url(path) if path.startswith("/") else path


def _images_cell(images: list) -> str:
	if not images:
		return ""
	shown = images[:_MAX_EXPORT_IMAGES]
	figures = []
	for i, img in enumerate(shown):
		caption = _IMAGE_CAPTIONS.get(img.image_type or "", img.image_type or "")
		figures.append(
			f'<figure class="{"main" if i == 0 else ""}"><img src="{_esc(_absolute_file_url(img.image))}" alt="{_esc(caption)}">'
			f"<figcaption>{_esc(caption)}</figcaption></figure>"
		)
	extra = len(images) - len(shown)
	more = f'<span class="more">+{extra} more</span>' if extra > 0 else ""
	return f'<div class="imgs-row">{"".join(figures)}{more}</div>'


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
	# Every image, not just the primary one: BRD C.14.1 has each catalog entry carry the
	# product image, the application/room image and the additional images. Primary first,
	# then the rest in the order staff arranged them.
	images_by_item: dict[str, list] = {}
	if item_codes:
		for row in frappe.get_all(
			"Product Image",
			filters={"parent": ["in", item_codes], "parenttype": "Item", "parentfield": "custom_images"},
			fields=["parent", "image", "image_type", "is_primary"],
			order_by="is_primary desc, idx asc",
		):
			if row.image:
				images_by_item.setdefault(row.parent, []).append(row)

	price_header = "<th class=\"price\">Price / box</th>" if include_price_flag else ""
	rows = []
	for it in items:
		price_cell = ""
		if include_price_flag:
			rate = get_price_for_dealer(it.name, dealer)
			price_cell = f"<td class=\"price\">{f'₹{rate:,.2f}' if rate is not None else '—'}</td>"
		thumb = _images_cell(images_by_item.get(it.name, []))
		dealer_code = dealer_codes.get(it.name)
		code_display = f"{_esc(it.name)}<br><span style=\"color:#8a9196\">{_esc(dealer_code)}</span>" if dealer_code else _esc(it.name)
		rows.append(
			f"""<tr>
	<td class="imgs">{thumb}</td>
	<td>{code_display}</td>
	<td>{_esc(it.item_name or '')}</td>
	<td>{_esc(it.custom_series or '—')}</td>
	<td>{_esc(it.custom_size or '—')} / {_esc(it.custom_finish or '—')}</td>
	{price_cell}
</tr>"""
		)

	table = f"""<table>
	<thead><tr><th></th><th>Item code</th><th>Name</th><th>Series</th><th>Size / Finish</th>{price_header}</tr></thead>
	<tbody>{''.join(rows) if rows else f'<tr><td colspan="{6 if include_price_flag else 5}">No items are currently visible in this dealer’s catalog.</td></tr>'}</tbody>
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
