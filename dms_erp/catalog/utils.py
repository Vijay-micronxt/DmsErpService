"""Shared constants/helpers for the Item (product) discontinuation lifecycle.

BRD §19 — discontinuation is a lifecycle, not a single flag: `is_reorderable` gates
whether Purchase can still raise POs for it (used by the Phase 4 reorder engine),
`is_sellable` gates whether Sales can still quote/sell remaining stock. Only "Pulled
Back" blocks a sale outright; the others sell down existing stock while cutting off
future purchase.
"""

import math

import frappe
from frappe import _

# 1 foot = 0.3048 m exactly (international definition), so 1 sqft = 0.3048**2 sqm
# exactly. Sq Ft and Sq Metre describe the same box area, not two independent
# item properties -- deriving one from the other (rather than storing a second
# per-item field a user could enter inconsistently with sqft_per_box) is the
# only way the two can never drift apart.
SQFT_TO_SQM = 0.09290304

MM_PER_FOOT = 304.8

# Sheet 7.8 -- an order/quotation line can be entered in whichever of these a
# dealer actually thinks in, not just boxes.
ORDER_UOMS = ["Box", "Pcs", "Sqft", "Sqm", "Kg", "Gram"]

DISCONTINUATION_STATUSES = [
	"Active",
	"Partially Discontinued",
	"Factory Discontinued",
	"Display Removal Pending",
	"Pulled Back",
]

_REORDERABLE = {"Active", "Partially Discontinued"}
_NOT_SELLABLE = {"Pulled Back"}


def is_reorderable(status: str) -> bool:
	return status in _REORDERABLE


def is_sellable(status: str) -> bool:
	return status not in _NOT_SELLABLE


def status_label(status: str) -> str:
	"""BRD C.10.5's client-facing label (e.g. Pacific's "PDC") for an internal
	discontinuation status -- white-labeled via DMS Catalog Settings, never the
	value is_reorderable/is_sellable/withdrawal_api match on or anything stores.
	No configured row (including every status on a fresh site) falls back to the
	raw internal value -- unconfigured is a no-op, not a Pacific-branded default."""
	rows = frappe.get_all(
		"DMS Catalog Status Label",
		filters={"parent": "DMS Catalog Settings", "status": status},
		fields=["label"],
		order_by="idx desc",
		limit=1,
	)
	return (rows[0].label if rows else None) or status


def item_weight_per_box_kg(item_code: str) -> float | None:
	"""BRD C.1.3: an item's standard weight — the fallback every transaction
	document uses before a real batch (with its own possibly-different
	custom_batch_weight_kg, see warehouse.utils.ensure_batch) is known."""
	return frappe.get_cached_value("Item", item_code, "custom_weight_per_box_kg")


def item_pieces_per_box(item_code: str) -> float | None:
	"""BRD C.1.3: Box->Pieces conversion factor. Unlike weight, this is a fixed
	geometric property of the tile/box (not something a batch can vary), so
	there's no batch-level override to check."""
	return frappe.get_cached_value("Item", item_code, "custom_pieces_per_box")


def item_sqft_per_box(item_code: str) -> float | None:
	"""BRD C.1.3: Box->Sq Ft conversion factor, same fixed-per-item reasoning as
	item_pieces_per_box."""
	return frappe.get_cached_value("Item", item_code, "custom_sqft_per_box")


def item_sqm_per_box(item_code: str) -> float | None:
	"""BRD C.1.3: Box->Sq Metre. Derived from sqft_per_box (see SQFT_TO_SQM) rather
	than its own stored field -- there is no separate "sqm_per_box" custom field on
	Item."""
	sqft_per_box = item_sqft_per_box(item_code)
	return round(sqft_per_box * SQFT_TO_SQM, 4) if sqft_per_box is not None else None


def item_default_supplier(item_code: str) -> str | None:
	"""BRD D.2: a Series carries the supplier/manufacturer that makes it (one Series
	= one supplier). An item's own custom_default_supplier -- settable independently
	of its Series, e.g. when Purchase re-sources just this one item elsewhere --
	always wins when set; only an item with neither (including one created before
	this field existed and never given a Series either) resolves to None."""
	default_supplier = frappe.get_cached_value("Item", item_code, "custom_default_supplier")
	if default_supplier:
		return default_supplier
	series_ref = frappe.get_cached_value("Item", item_code, "custom_series_ref")
	return frappe.get_cached_value("Product Series", series_ref, "supplier") if series_ref else None


def derive_sqft_per_box(height_mm: float | None, width_mm: float | None, pieces_per_box: float | None) -> float | None:
	"""BRD sheet item -- Height/Width (mm) are the physical tile dimensions; when
	both are known along with pieces_per_box, sqft_per_box doesn't need to be
	typed in separately and risk drifting from the real tile size. Returns None
	(never a guess) when any of the three inputs is missing."""
	if not height_mm or not width_mm or not pieces_per_box:
		return None
	return round((height_mm / MM_PER_FOOT) * (width_mm / MM_PER_FOOT) * pieces_per_box, 4)


def convert_qty_to_boxes(item_code: str, qty: float, uom: str) -> float:
	"""Sheet 7.8 -- the single conversion point from whatever unit an order/
	quotation line was entered in back to boxes, the one unit every downstream
	rule (pricing, channel/credit-limit thresholds, stock, picking, invoicing)
	actually operates on. Called once, at order/quotation creation, before any
	of those ever sees the line's qty (see sales.order_api.create_order and
	sales.quotation_api.create_quotation).

	Whatever doesn't divide evenly into a whole box (50 sqft when a box covers
	18, say) rounds UP -- a dealer can end up with slightly more tile than
	asked for, never less, and a box can't be physically split to undershoot."""
	if uom not in ORDER_UOMS:
		frappe.throw(_("Unknown unit {0}.").format(uom), frappe.ValidationError)
	qty = float(qty)
	if uom == "Box":
		return qty

	weight_per_box_kg = item_weight_per_box_kg(item_code)
	per_box = {
		"Pcs": item_pieces_per_box(item_code),
		"Sqft": item_sqft_per_box(item_code),
		"Sqm": item_sqm_per_box(item_code),
		"Kg": weight_per_box_kg,
		"Gram": weight_per_box_kg * 1000 if weight_per_box_kg else None,
	}[uom]
	if not per_box:
		frappe.throw(
			_("{0} has no {1}-per-box figure set, so an order can't be entered in {1} for it.").format(item_code, uom),
			frappe.ValidationError,
		)
	return math.ceil(qty / per_box)


def normalize_order_line_uoms(lines: list[dict]) -> list[dict]:
	"""Runs convert_qty_to_boxes over every line once. Returns new line dicts --
	`qty` replaced with the box-equivalent; the originally entered qty/uom
	survive as `entered_qty`/`entered_uom` only when a non-Box unit was actually
	used (left out entirely for a plain box order, so nothing extra shows up on
	the document for the common case)."""
	out = []
	for line in lines:
		uom = line.get("uom") or "Box"
		entered_qty = float(line["qty"])
		normalized = {**line, "qty": convert_qty_to_boxes(line["item"], entered_qty, uom)}
		if uom != "Box":
			normalized["entered_qty"] = entered_qty
			normalized["entered_uom"] = uom
		out.append(normalized)
	return out
