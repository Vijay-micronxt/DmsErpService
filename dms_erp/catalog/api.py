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

import frappe
from frappe import _

from dms_erp.catalog.utils import DISCONTINUATION_STATUSES, is_reorderable, is_sellable
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
		"leadTimeDays": item_doc.lead_time_days,
		"altItemId": _get_alt_item(item_doc.name),
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
	size: str | None = None,
	finish: str | None = None,
	color: str | None = None,
	series: str | None = None,
	swatch: str | None = None,
	status: str = "Active",
	pieces_per_box: float = 0,
	sqft_per_box: float = 0,
	weight_per_box_kg: float = 0,
	lead_time_days: int = 0,
	alt_item: str | None = None,
	hsn_code: str | None = None,
):
	_assert_can_manage_products()

	if status not in DISCONTINUATION_STATUSES:
		frappe.throw(_("Invalid status: {0}").format(status), frappe.ValidationError)

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
			"custom_series": series,
			"custom_swatch_color": swatch,
			"custom_discontinuation_status": status,
			"custom_pieces_per_box": pieces_per_box,
			"custom_sqft_per_box": sqft_per_box,
			"custom_weight_per_box_kg": weight_per_box_kg,
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

	field_map = {
		"name": "item_name",
		"category": "item_group",
		"size": "custom_size",
		"finish": "custom_finish",
		"color": "custom_color",
		"series": "custom_series",
		"swatch": "custom_swatch_color",
		"status": "custom_discontinuation_status",
		"piecesPerBox": "custom_pieces_per_box",
		"sqftPerBox": "custom_sqft_per_box",
		"weightPerBoxKg": "custom_weight_per_box_kg",
		"leadTimeDays": "lead_time_days",
		"hsnCode": "gst_hsn_code",
	}

	doc = frappe.get_doc("Item", item)
	for key, value in patch.items():
		if key == "altItemId":
			_set_alt_item(doc.name, value)
			continue
		fieldname = field_map.get(key)
		if fieldname:
			doc.set(fieldname, value)
	doc.save(ignore_permissions=True)

	return _serialize(doc)


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
