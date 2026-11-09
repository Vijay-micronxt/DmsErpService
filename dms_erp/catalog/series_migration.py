"""One-time (but safely re-runnable) migration: link every Item to a real
Product Series, derived from the Tally-migrated Item Group it already
belongs to.

This finishes a migration step the BRD itself already called for and that
was apparently never completed: F.3.2 step 4 ("Map items to the new Series
structure where possible") and F.3.3's own row for "Item attributes embedded
in item name/series (size, thickness, finish, series)" -> "Item attribute
fields + Series master." A live check (7 Nov 2026) found zero of the 4,340
real items linked to any Product Series at all -- this is that gap, closed.

Why Item Group decides the grouping, not item-name parsing: Item Group is
already a controlled field, assigned to every item during the original
migration -- not something to re-derive or guess (series_api.py's own module
docstring already treats "every SKU in Pacific's 'Marbello' range" as the
textbook example of what a Series is, which is exactly what an Item Group
like "Simero 600x1200 Discontinued Tiles - 02 Pcs" already is in practice).
Only the *used* Item Groups matter (655 of 1,207 total records are actually
on an item) -- the other 552 are dead records from the original import.

Size/Pieces-per-box need no extraction at all -- confirmed already correct,
per item, from the original migration; this only copies whichever value a
group's own items agree on up onto the shared Series record.

Finish/Sub Finish are the one thing with no structured field from Tally.
Pulled from each item's own name by matching a short, known word list
(reusing series_api's own FINISH_DEFAULTS/SUB_FINISH_DEFAULTS vocabulary,
checked first against this catalog's real word frequency -- see the
conversation this came out of, 7 Nov 2026). PGVT is checked before GVT so a
PGVT item isn't also wrongly matched as GVT (GVT is a substring of PGVT).
A handful of real, frequently-repeated name prefixes (LGV, SGH, MSM, MCT,
SCA, MTH, MGH, STO, ILC, DVS, SPF, DV, DG, FL, PDC, LXT) are NOT on this
list -- they're almost certainly brand/collection codes, not alternate
finish words, but nobody at Pacific has confirmed that yet, so they're left
alone rather than guessed. Finish is set directly per item (Item has its own
custom_finish field); Sub Finish only has a Series-level field, so it's set
there as that series' most common one, not per item.

Also backfills custom_height_mm/custom_width_mm from the already-real
tally_height_mm/tally_width_mm (present on 4,229/4,340 items, but never
copied into the actual structured fields), then derives custom_sqft_per_box
from them via catalog.utils.derive_sqft_per_box -- the same helper every
other item-creation path already uses, not new math.

Idempotent by design: an item that already has custom_series_ref set is left
untouched; a Series that already exists for a given Item Group is reused,
never duplicated. Run with dry_run=True (the default) to get a full report
with nothing written -- real writes only happen with dry_run=False.
"""

import re
from collections import Counter

import frappe

from dms_erp.catalog.utils import derive_sqft_per_box

# ERPNext's generic root category ("All Item Groups") plus the handful of
# broad placeholder categories catalog.setup.py itself seeds (Vitrified/Floor
# Tiles/Wall Tiles/Outdoor-Parking) -- confirmed (7 Nov 2026 dry run) sitting
# on 58 real items with no real specific category ever assigned. These are
# not series, just items that never got a proper Item Group during the
# original migration; treated the same as a blank Item Group (skipped, not
# turned into a fake "Series" named "All Item Groups").
GENERIC_ITEM_GROUPS = {"All Item Groups", "Vitrified", "Floor Tiles", "Wall Tiles", "Outdoor / Parking"}

FINISH_WORDS = ["Matt", "Glossy", "Polished", "Satin"]
# PGVT before GVT: GVT is a substring of PGVT, and `_find_word`'s word-boundary
# match alone can't tell "this GVT is actually part of a PGVT" apart from a
# genuinely standalone GVT unless the longer word is tried first.
SUB_FINISH_WORDS = ["PGVT", "GVT", "Carving", "Rocker", "Stone-Art"]

_BASE_ITEM_FIELDS = [
	"name",
	"item_name",
	"item_group",
	"custom_size",
	"custom_pieces_per_box",
	"custom_weight_per_box_kg",
	"custom_sqft_per_box",
	"custom_height_mm",
	"custom_width_mm",
	"custom_finish",
	"custom_series_ref",
]

# tally_height_mm/tally_width_mm are NOT a dms_erp fixture -- they're an ad hoc
# column left over on whichever site actually went through the original Tally
# migration (same situation as patches/backfill_pieces_per_box_from_tally.py's
# own tally_pieces_per_box guard). A fresh demo site with no Tally import has
# no such column at all; selecting it unconditionally would crash `bench
# migrate` there with "Unknown column 'tally_height_mm'".
_TALLY_DIM_FIELDS = ["tally_height_mm", "tally_width_mm"]


def _find_word(text: str, words: list[str]) -> str | None:
	for word in words:
		pattern = word.replace("-", r"[-\s]?")
		if re.search(r"\b" + pattern + r"\b", text or "", re.IGNORECASE):
			return word
	return None


def _most_common(values: list) -> tuple[object | None, int, int]:
	"""(winning value, votes for it, total non-empty votes) -- None when there
	were no non-empty values to vote with at all."""
	counts = Counter(v for v in values if v not in (None, "", 0, 0.0))
	if not counts:
		return None, 0, 0
	value, votes = counts.most_common(1)[0]
	return value, votes, sum(counts.values())


def run(dry_run: bool = True) -> dict:
	report = {
		"dry_run": dry_run,
		"series_created": 0,
		"series_reused": 0,
		"items_linked": 0,
		"items_skipped_already_linked": 0,
		"items_skipped_no_item_group": 0,
		"items_skipped_generic_group": 0,
		"items_finish_set": 0,
		"items_height_width_backfilled": 0,
		"items_sqft_derived": 0,
		"size_disagreements": [],
		"pieces_per_box_disagreements": [],
	}

	has_tally_dims = all(frappe.db.has_column("Item", f) for f in _TALLY_DIM_FIELDS)
	fields = _BASE_ITEM_FIELDS + (_TALLY_DIM_FIELDS if has_tally_dims else [])
	items = frappe.get_all("Item", fields=fields)

	groups: dict[str, list[dict]] = {}
	for item in items:
		if item.custom_series_ref:
			report["items_skipped_already_linked"] += 1
		elif not item.item_group:
			report["items_skipped_no_item_group"] += 1
		elif item.item_group in GENERIC_ITEM_GROUPS:
			report["items_skipped_generic_group"] += 1
		else:
			groups.setdefault(item.item_group, []).append(item)

	for item_group, group_items in groups.items():
		size, size_votes, size_total = _most_common([i.custom_size for i in group_items])
		if size_total and size_votes < size_total:
			report["size_disagreements"].append(
				{"itemGroup": item_group, "chosen": size, "agreeing": size_votes, "total": size_total}
			)

		pieces_per_box, pcs_votes, pcs_total = _most_common([i.custom_pieces_per_box for i in group_items])
		if pcs_total and pcs_votes < pcs_total:
			report["pieces_per_box_disagreements"].append(
				{"itemGroup": item_group, "chosen": pieces_per_box, "agreeing": pcs_votes, "total": pcs_total}
			)

		weight_per_box_kg, _, _ = _most_common([i.custom_weight_per_box_kg for i in group_items])

		# Per-item finish/sub-finish/height-width backfill, computed here so the
		# series' own finish/sub_finish/sqft can vote on the *resulting* values.
		item_finishes = []
		item_sub_finishes = []
		item_sqfts = []
		for item in group_items:
			finish = _find_word(item.item_name, FINISH_WORDS)
			if finish:
				item_finishes.append(finish)
				if not item.custom_finish:
					report["items_finish_set"] += 1
					if not dry_run:
						frappe.db.set_value("Item", item.name, "custom_finish", finish, update_modified=False)

			sub_finish = _find_word(item.item_name, SUB_FINISH_WORDS)
			if sub_finish:
				item_sub_finishes.append(sub_finish)

			tally_height_mm = item.get("tally_height_mm")
			tally_width_mm = item.get("tally_width_mm")
			height_mm = item.custom_height_mm or tally_height_mm
			width_mm = item.custom_width_mm or tally_width_mm
			if not item.custom_height_mm and not item.custom_width_mm and tally_height_mm and tally_width_mm:
				report["items_height_width_backfilled"] += 1
				if not dry_run:
					frappe.db.set_value(
						"Item",
						item.name,
						{"custom_height_mm": tally_height_mm, "custom_width_mm": tally_width_mm},
						update_modified=False,
					)

			sqft = item.custom_sqft_per_box or derive_sqft_per_box(height_mm, width_mm, item.custom_pieces_per_box)
			if sqft:
				item_sqfts.append(sqft)
				if not item.custom_sqft_per_box:
					report["items_sqft_derived"] += 1
					if not dry_run:
						frappe.db.set_value("Item", item.name, "custom_sqft_per_box", sqft, update_modified=False)

		finish, _, _ = _most_common(item_finishes)
		sub_finish, _, _ = _most_common(item_sub_finishes)
		sqft_per_box, _, _ = _most_common(item_sqfts)

		series_name = item_group
		if frappe.db.exists("Product Series", series_name):
			report["series_reused"] += 1
		else:
			report["series_created"] += 1
			if not dry_run:
				frappe.get_doc(
					{
						"doctype": "Product Series",
						"series_name": series_name,
						"size": size,
						"finish": finish,
						"sub_finish": sub_finish,
						"pieces_per_box": pieces_per_box,
						"sqft_per_box": sqft_per_box,
						"weight_per_box_kg": weight_per_box_kg,
					}
				).insert(ignore_permissions=True)

		report["items_linked"] += len(group_items)
		if not dry_run:
			for item in group_items:
				frappe.db.set_value("Item", item.name, "custom_series_ref", series_name, update_modified=False)

	if not dry_run:
		frappe.db.commit()

	return report
