"""dms_erp's "Series" doctype collided with Frappe's own internal `tabSeries` table
(the naming-series counter table: name, current), so inserts failed with "Unknown
column 'owner'" and our columns were bolted onto Frappe's counter table. The doctype
is now "Product Series".

Step 1 of 2 (pre_model_sync): remove the stale DocType record so migrate doesn't try to
manage it any more. Data is untouched here — copy_series_to_product_series (post_model_sync)
moves any real rows once the new table exists.

Deliberately does NOT use frappe.delete_doc, which would DROP TABLE `tabSeries` and
destroy every real naming counter.
"""

import frappe


def execute():
	if frappe.db.get_value("DocType", "Series", "module") != "Catalog":
		return

	frappe.db.delete("DocField", {"parent": "Series", "parenttype": "DocType"})
	frappe.db.delete("DocPerm", {"parent": "Series", "parenttype": "DocType"})
	frappe.db.delete("DocType", {"name": "Series"})
	frappe.clear_cache()
