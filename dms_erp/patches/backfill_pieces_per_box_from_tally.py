"""One-time backfill for Item.custom_pieces_per_box, 30 Oct 2026 -- the field the app
actually reads everywhere (catalog.api.get_product, dealer_portal_api._catalog_entry,
stock-format's "X Box Y pcs" display) was never copied from tally_pieces_per_box (the
raw value preserved from the original Tally import) on at least one real deployment,
even though both fields exist and agree everywhere the copy *did* happen. Confirmed by
diffing a raw Tally migration backup against that deployment for the same item codes --
tally_pieces_per_box was correct in both, custom_pieces_per_box was 0 only on the live
site. Only fills rows that are currently empty and have a real Tally value to copy from;
never touches a row that's already been explicitly set, and never invents a number for
an item Tally itself never had one for (those stay 0, same as today -- no honest value
to put there).
"""

import frappe


def execute():
	frappe.db.sql(
		"""
		update `tabItem`
		set custom_pieces_per_box = tally_pieces_per_box
		where (custom_pieces_per_box is null or custom_pieces_per_box = 0)
			and tally_pieces_per_box > 0
		"""
	)
