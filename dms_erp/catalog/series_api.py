"""Series master (BRD C.1.1) — the central master concept the BRD describes: a
series carries a supplier, tile attributes (size/thickness/finish), UOM conversions
and retail/bulk qty thresholds, and Items belonging to it inherit those as defaults.

Item Group already covers "category" (Vitrified/Floor Tiles/...); Series is a
narrower, more specific grouping *within* a category (e.g. every SKU in Pacific's
"Marbello" range), which is why it's a new doctype rather than reusing Item Group.

Series doesn't publish prices directly — its `price_list_rates` child table is the
source Batch 2's dealer price-tier work (BRD C.1.4/C.7.1) reads from once that
lands; nothing in this module writes an Item Price yet.
"""

import frappe
from frappe import _

from dms_erp.pagination import clamp

SERIES_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}


def _assert_can_manage_series():
	if not set(frappe.get_roles(frappe.session.user)) & SERIES_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage the Series master."), frappe.PermissionError)


def _serialize(doc: "frappe.model.document.Document") -> dict:
	return {
		"id": doc.name,
		"seriesName": doc.series_name,
		"supplier": doc.supplier,
		"size": doc.size,
		"thickness": doc.thickness,
		"finish": doc.finish,
		"piecesPerBox": doc.pieces_per_box,
		"sqftPerBox": doc.sqft_per_box,
		"weightPerBoxKg": doc.weight_per_box_kg,
		"bulkQtyThreshold": doc.bulk_qty_threshold,
		"retailQtyThreshold": doc.retail_qty_threshold,
		"priceListRates": [{"priceList": row.price_list, "rate": row.rate} for row in doc.price_list_rates],
	}


@frappe.whitelist(methods=["GET"])
def list_series(search: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = {"series_name": ["like", f"%{search}%"]} if search else {}
	total = frappe.db.count("Series", filters=filters)
	names = frappe.get_all("Series", filters=filters, pluck="name", order_by="series_name asc", limit_start=offset, limit_page_length=limit)
	return {
		"items": [_serialize(frappe.get_doc("Series", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_series(series: str):
	return _serialize(frappe.get_doc("Series", series))


@frappe.whitelist(methods=["POST"])
def create_series(
	series_name: str,
	supplier: str | None = None,
	size: str | None = None,
	thickness: str | None = None,
	finish: str | None = None,
	pieces_per_box: float = 0,
	sqft_per_box: float = 0,
	weight_per_box_kg: float = 0,
	bulk_qty_threshold: int = 0,
	retail_qty_threshold: int = 0,
	price_list_rates: list[dict] | None = None,
):
	_assert_can_manage_series()

	doc = frappe.get_doc(
		{
			"doctype": "Series",
			"series_name": series_name,
			"supplier": supplier,
			"size": size,
			"thickness": thickness,
			"finish": finish,
			"pieces_per_box": pieces_per_box,
			"sqft_per_box": sqft_per_box,
			"weight_per_box_kg": weight_per_box_kg,
			"bulk_qty_threshold": bulk_qty_threshold,
			"retail_qty_threshold": retail_qty_threshold,
			"price_list_rates": price_list_rates or [],
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_series(series: str, patch: dict):
	_assert_can_manage_series()

	field_map = {
		"supplier": "supplier",
		"size": "size",
		"thickness": "thickness",
		"finish": "finish",
		"piecesPerBox": "pieces_per_box",
		"sqftPerBox": "sqft_per_box",
		"weightPerBoxKg": "weight_per_box_kg",
		"bulkQtyThreshold": "bulk_qty_threshold",
		"retailQtyThreshold": "retail_qty_threshold",
		"priceListRates": "price_list_rates",
	}

	doc = frappe.get_doc("Series", series)
	for key, value in patch.items():
		fieldname = field_map.get(key)
		if fieldname:
			doc.set(fieldname, value)
	doc.save(ignore_permissions=True)
	return _serialize(doc)
