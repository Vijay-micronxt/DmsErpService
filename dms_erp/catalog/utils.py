"""Shared constants/helpers for the Item (product) discontinuation lifecycle.

BRD §19 — discontinuation is a lifecycle, not a single flag: `is_reorderable` gates
whether Purchase can still raise POs for it (used by the Phase 4 reorder engine),
`is_sellable` gates whether Sales can still quote/sell remaining stock. Only "Pulled
Back" blocks a sale outright; the others sell down existing stock while cutting off
future purchase.
"""

import frappe

# 1 foot = 0.3048 m exactly (international definition), so 1 sqft = 0.3048**2 sqm
# exactly. Sq Ft and Sq Metre describe the same box area, not two independent
# item properties -- deriving one from the other (rather than storing a second
# per-item field a user could enter inconsistently with sqft_per_box) is the
# only way the two can never drift apart.
SQFT_TO_SQM = 0.09290304

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
