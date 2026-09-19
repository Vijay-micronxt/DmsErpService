"""Step 2 of 2 (post_model_sync, once `tabProduct Series` exists): carry over any real
Series records that were stored on the old colliding `tabSeries` table, then clean up.

A real Series row is one with `series_name` set — Frappe's own naming counters never have
our columns filled in. Rows are copied first, and only then are they (and the stray
columns) removed from `tabSeries`, so nothing is lost. Frappe's real counter rows and its
`name`/`current` columns are never touched.
"""

import frappe

STRAY_COLUMNS = [
	"series_name",
	"supplier",
	"size",
	"thickness",
	"finish",
	"pieces_per_box",
	"sqft_per_box",
	"weight_per_box_kg",
	"bulk_qty_threshold",
	"retail_qty_threshold",
	"withdrawal_automation_enabled",
	"withdrawal_no_sale_days_threshold",
	"withdrawal_min_store_count",
	"withdrawal_min_annual_sales_boxes",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
]

# Standard doctype columns the old table may not have (it never got them, which is why
# inserts failed) — filled with sensible values if a row is being carried over anyway.
STANDARD_DEFAULTS = {
	"owner": "'Administrator'",
	"modified_by": "'Administrator'",
	"creation": "now(6)",
	"modified": "now(6)",
	"docstatus": "0",
	"idx": "0",
}


def execute():
	if not frappe.db.has_column("Series", "series_name"):
		return

	source_columns = set(frappe.db.get_table_columns("Series"))
	target_columns = frappe.db.get_table_columns("Product Series")

	shared = [column for column in target_columns if column in source_columns]
	filled = {column: value for column, value in STANDARD_DEFAULTS.items() if column in target_columns and column not in source_columns}

	target_list = ", ".join(f"`{column}`" for column in [*shared, *filled])
	select_list = ", ".join([*(f"`{column}`" for column in shared), *filled.values()])
	frappe.db.sql(
		f"insert ignore into `tabProduct Series` ({target_list}) select {select_list} from `tabSeries` where `series_name` is not null"
	)
	frappe.db.sql("delete from `tabSeries` where `series_name` is not null")

	for column in STRAY_COLUMNS:
		if frappe.db.has_column("Series", column):
			frappe.db.sql_ddl(f"alter table `tabSeries` drop column `{column}`")

	frappe.db.set_value("Custom Field", {"dt": "Item", "fieldname": "custom_series_ref"}, "options", "Product Series")
	frappe.db.sql("update `tabSeries Price List Rate` set parenttype = 'Product Series' where parenttype = 'Series'")
	frappe.clear_cache()
