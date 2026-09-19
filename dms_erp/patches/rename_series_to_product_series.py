"""dms_erp's "Series" doctype collided with Frappe's own internal `tabSeries` table
(the naming-series counter table: name, current), so inserts failed with "Unknown
column 'owner'" and our columns were bolted onto Frappe's counter table. The doctype
is now "Product Series".

This removes the stale DocType record and the stray columns from `tabSeries` on sites
that already ran the old version. It deliberately does NOT use frappe.delete_doc, which
would DROP TABLE `tabSeries` and destroy every real naming counter.
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


def execute():
	if frappe.db.get_value("DocType", "Series", "module") != "Catalog":
		return

	for column in STRAY_COLUMNS:
		if frappe.db.has_column("Series", column):
			frappe.db.sql_ddl(f"alter table `tabSeries` drop column `{column}`")

	frappe.db.delete("DocField", {"parent": "Series", "parenttype": "DocType"})
	frappe.db.delete("DocPerm", {"parent": "Series", "parenttype": "DocType"})
	frappe.db.delete("DocType", {"name": "Series"})

	frappe.db.set_value(
		"Custom Field", {"dt": "Item", "fieldname": "custom_series_ref"}, "options", "Product Series"
	)
	frappe.db.sql("update `tabSeries Price List Rate` set parenttype = 'Product Series' where parenttype = 'Series'")
	frappe.clear_cache()
