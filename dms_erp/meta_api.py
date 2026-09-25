"""Configurable list-page columns — brings ERPNext's Report View "Pick Columns" /
Grid "Configure Columns" behavior to the staff frontend without hand-maintaining
a second column list per page.

Frappe already carries everything a column picker needs on DocField --
fieldname, label, fieldtype, in_list_view (ERPNext's default ~10-column cap),
columns (the Grid's "Column Weight"), and idx (natural field order) -- merged
with our own Custom Field rows via frappe.get_meta(). A field added to a
doctype's own .json, or a Custom Field added in a module's setup.py, shows up
here automatically; nothing here needs to change when that happens.

Top-level (not under sales/ or warehouse/), same reasoning as phone_utils.py/
qr_utils.py: this is genuinely cross-module, not owned by any one of them.
"""

import frappe
from frappe import _

# The doctypes this app's list pages render as tables. Extend as new pages
# adopt the column picker -- never accept an arbitrary caller-supplied
# doctype here. This endpoint only exposes schema (field names/labels/
# types), not data, but a staff list-page column picker still has no reason
# to be able to fingerprint doctypes it never renders (User, Role, ...).
COLUMN_OPTION_DOCTYPES = {
	"Inquiry",
	"Insurance Claim",
	"Sales Order",
	"Customer",
	"Supplier",
	"Warehouse",
	"Purchase Order",
}

# Layout/structural fieldtypes carry no data of their own -- never valid
# columns.
NON_DATA_FIELDTYPES = {
	"Section Break",
	"Column Break",
	"Tab Break",
	"HTML",
	"Button",
	"Heading",
	"Fold",
	"Table",
	"Table MultiSelect",
}


@frappe.whitelist(methods=["GET"])
def get_column_options(doctype: str):
	if doctype not in COLUMN_OPTION_DOCTYPES:
		frappe.throw(_("{0} does not support configurable columns.").format(doctype), frappe.ValidationError)
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(_("Not permitted to read {0}.").format(doctype), frappe.PermissionError)

	meta = frappe.get_meta(doctype)
	options = [
		{
			"fieldname": field.fieldname,
			"label": field.label or field.fieldname,
			"fieldtype": field.fieldtype,
			"defaultVisible": bool(field.in_list_view),
			"weight": field.columns or 1,
			"order": field.idx,
		}
		for field in meta.fields
		if field.fieldtype not in NON_DATA_FIELDTYPES and not field.hidden
	]
	options.sort(key=lambda f: f["order"])
	return options
