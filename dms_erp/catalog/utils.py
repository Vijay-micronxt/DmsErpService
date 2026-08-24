"""Shared constants/helpers for the Item (product) discontinuation lifecycle.

BRD §19 — discontinuation is a lifecycle, not a single flag: `is_reorderable` gates
whether Purchase can still raise POs for it (used by the Phase 4 reorder engine),
`is_sellable` gates whether Sales can still quote/sell remaining stock. Only "Pulled
Back" blocks a sale outright; the others sell down existing stock while cutting off
future purchase.
"""

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
