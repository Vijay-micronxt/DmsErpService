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

tally_pieces_per_box isn't a dms_erp fixture/custom field -- it's an ad hoc column left
over on that one deployment's database from its original Tally migration, not something
this app ships or creates anywhere. A site that never went through that Tally import
(e.g. a fresh demo site) simply doesn't have the column, so the bare UPDATE below broke
`bench migrate` there with "Unknown column 'tally_pieces_per_box'" (reported on tas-demo,
3 Nov 2026). Guarded the same way the rest of this patch is already honest about missing
data: nothing to copy on a site without that column, so skip rather than crash.
"""

import frappe


def execute():
	if not frappe.db.has_column("Item", "tally_pieces_per_box"):
		return

	frappe.db.sql(
		"""
		update `tabItem`
		set custom_pieces_per_box = tally_pieces_per_box
		where (custom_pieces_per_box is null or custom_pieces_per_box = 0)
			and tally_pieces_per_box > 0
		"""
	)
