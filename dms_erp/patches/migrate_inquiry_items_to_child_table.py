"""One-time backfill for the 12 Nov 2026 Inquiry rebuild (BRD D.3): Inquiry
used to store exactly one item as scalar columns (item, qty, entered_boxes,
entered_qty, entered_uom, damaged_stock, duplicate_flag, preferred_batch_no,
custom_requested_style) directly on the parent, and its old Select status
field conflated two concerns -- workflow stage and per-item stock
availability -- in one value. Those columns move to a new Inquiry Item child
row, and the old combined status splits into the parent's own workflow-only
status plus the new child row's availability_status.

This only reshapes each existing record into itself with one child row -- it
deliberately does NOT merge records that share the old, now-dropped
request_group into a single multi-item Inquiry. Nothing on those sibling
records proves they were ever meant to become one Inquiry rather than several
raised together by coincidence, and each already carries its own independent
status/linked_sales_order/linked_quotation that a merge could stomp on or
orphan. Going forward, genuinely multi-item Inquiries are created directly as
such by inquiry_api._create_inquiry_multi; this patch just keeps historical
data readable under the new schema, one real row each.

Idempotent -- skips any Inquiry that already has an Inquiry Item child row,
so a second `bench migrate` run (or a manual re-run) is a no-op.
"""

import frappe

# The four stock-availability values the old Inquiry.status field used to
# carry alongside its workflow values -- moving forward these only ever live
# on Inquiry Item.availability_status.
OLD_AVAILABILITY_STATUSES = {"Available", "Partially Available", "Out of Stock", "Pre-order Required"}


def execute():
	if not frappe.db.table_exists("Inquiry Item"):
		return
	columns = set(frappe.db.get_table_columns("Inquiry"))
	if "item" not in columns:
		return  # already dropped from the parent table -- nothing left to migrate from

	# duplicate_flag only ever existed on a handful of dev/test sites as a
	# transient step of this same rebuild (added to the old parent doctype,
	# then moved here to the child one before anything was ever committed) --
	# a real site going straight from the last released schema to this one
	# never had it on `tabInquiry` at all, so it's selected only when present
	# rather than assumed, the same way the rebuild's own code never assumed
	# it existed before adding it properly (see inquiry_item.json's own note).
	has_duplicate_flag = "duplicate_flag" in columns
	duplicate_flag_select = ", duplicate_flag" if has_duplicate_flag else ""

	rows = frappe.db.sql(
		f"""
		select name, item, custom_requested_style, qty, entered_boxes, entered_qty,
			entered_uom, damaged_stock, preferred_batch_no, status{duplicate_flag_select}
		from `tabInquiry`
		where coalesce(item, '') != '' or coalesce(custom_requested_style, '') != ''
		""",
		as_dict=True,
	)

	for row in rows:
		if frappe.db.exists("Inquiry Item", {"parent": row.name, "parenttype": "Inquiry"}):
			continue

		availability_status = row.status if row.status in OLD_AVAILABILITY_STATUSES else "Open"
		new_status = "Open" if row.status in OLD_AVAILABILITY_STATUSES else row.status

		child = frappe.get_doc(
			{
				"doctype": "Inquiry Item",
				"parent": row.name,
				"parenttype": "Inquiry",
				"parentfield": "items",
				"idx": 1,
				"item": row.item,
				"custom_requested_style": row.custom_requested_style,
				"qty": row.qty,
				"entered_boxes": row.entered_boxes,
				"entered_qty": row.entered_qty,
				"entered_uom": row.entered_uom,
				"damaged_stock": row.damaged_stock,
				"duplicate_flag": row.get("duplicate_flag") or 0,
				"preferred_batch_no": row.preferred_batch_no,
				"availability_status": availability_status,
			}
		)
		child.insert(ignore_permissions=True)

		if new_status != row.status:
			frappe.db.set_value("Inquiry", row.name, "status", new_status, update_modified=False)
