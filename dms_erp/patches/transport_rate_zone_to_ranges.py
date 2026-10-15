"""Transport Rate Zone moved from one exact pincode per row (field `pincode`, with
`suggested_freight`) to a pincode range with a rate basis (ISS-2026-00633, 14 Oct
2026). Frappe never drops a removed field's column, so any row saved under the old
shape still has its pincode (also its name) and freight in the table; this carries
them into the new fields as a one-pincode range at a flat Per Trip rate -- exactly
what the old exact-match lookup meant. Rows already in the new shape are skipped.
"""

import frappe


def execute():
	columns = frappe.db.get_table_columns("Transport Rate Zone")
	freight_col = "suggested_freight" if "suggested_freight" in columns else None
	rows = frappe.db.sql(
		f"""
		select name{", " + freight_col if freight_col else ""}
		from `tabTransport Rate Zone`
		where coalesce(from_pincode, '') = ''
		""",
		as_dict=True,
	)
	for row in rows:
		frappe.db.set_value(
			"Transport Rate Zone",
			row.name,
			{
				"zone_name": row.name,
				"from_pincode": row.name,
				"to_pincode": row.name,
				"rate_basis": "Per Trip",
				"rate": row.get("suggested_freight") or 0,
			},
			update_modified=False,
		)
