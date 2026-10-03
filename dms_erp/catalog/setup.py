"""Idempotent install/migrate-time setup for the catalog module.

Item is a core ERPNext doctype we don't own, so Pacific-specific attributes are
added as Custom Fields rather than editing Item's own JSON. Item Group is used
as-is for `category` (native equivalent, no custom doctype needed). altItemId
from the frontend's Product type maps onto ERPNext's own Item Alternative
doctype (two_way=1), so no field or doctype is needed for it either.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from dms_erp.catalog.utils import DISCONTINUATION_STATUSES

# Seeded from the categories already in use across the pacific-tileflow mock data.
ITEM_GROUPS = ["Vitrified", "Floor Tiles", "Wall Tiles", "Outdoor / Parking"]

CUSTOM_FIELDS = {
	"Item": [
		{
			"fieldname": "dms_attributes_section",
			"fieldtype": "Section Break",
			"label": "DMS Tile Attributes",
			"insert_after": "item_group",
		},
		{"fieldname": "custom_size", "fieldtype": "Data", "label": "Size", "insert_after": "dms_attributes_section"},
		{"fieldname": "custom_finish", "fieldtype": "Data", "label": "Finish", "insert_after": "custom_size"},
		{"fieldname": "custom_color", "fieldtype": "Data", "label": "Color", "insert_after": "custom_finish"},
		{"fieldname": "custom_series", "fieldtype": "Data", "label": "Series", "insert_after": "custom_color"},
		{
			"fieldname": "custom_series_ref",
			"fieldtype": "Link",
			"label": "Series (Master)",
			"options": "Product Series",
			"description": "BRD C.1.1 central Series master. When set at item creation, unset attribute/UOM/threshold fields are filled in from this Series (explicit values passed at creation always win).",
			"insert_after": "custom_series",
		},
		{
			"fieldname": "custom_bulk_qty_threshold",
			"fieldtype": "Int",
			"label": "Bulk Qty Threshold (Boxes)",
			"insert_after": "custom_series_ref",
		},
		{
			"fieldname": "custom_retail_qty_threshold",
			"fieldtype": "Int",
			"label": "Retail Qty Threshold (Boxes)",
			"insert_after": "custom_bulk_qty_threshold",
		},
		{
			"fieldname": "custom_swatch_color",
			"fieldtype": "Data",
			"label": "Swatch Color (CSS)",
			"description": "e.g. oklch(0.93 0.01 250) — used for the color swatch in the staff app UI.",
			"insert_after": "custom_retail_qty_threshold",
		},
		{"fieldname": "dms_attributes_column_break", "fieldtype": "Column Break", "insert_after": "custom_swatch_color"},
		{"fieldname": "custom_pieces_per_box", "fieldtype": "Float", "label": "Pieces per Box", "insert_after": "dms_attributes_column_break"},
		{"fieldname": "custom_sqft_per_box", "fieldtype": "Float", "label": "Sqft per Box", "insert_after": "custom_pieces_per_box"},
		{"fieldname": "custom_weight_per_box_kg", "fieldtype": "Float", "label": "Weight per Box (Kg)", "insert_after": "custom_sqft_per_box"},
		{
			"fieldname": "custom_discontinuation_status",
			"fieldtype": "Select",
			"label": "Discontinuation Status",
			"options": "\n".join(DISCONTINUATION_STATUSES),
			"default": "Active",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"insert_after": "custom_weight_per_box_kg",
		},
		{
			"fieldname": "custom_moq",
			"fieldtype": "Int",
			"label": "MOQ (Boxes)",
			"description": "BRD C.4.2 -- stock MOQ for this item. Falls back to the item's default Supplier's own MOQ, then DMS Purchase Settings' site-wide default when unset at either level.",
			"insert_after": "custom_discontinuation_status",
		},
		{
			"fieldname": "custom_production_moq",
			"fieldtype": "Int",
			"label": "Production MOQ (Boxes)",
			"description": "BRD C.4.2 -- \"production MOQ may differ from stock MOQ\": the floor once a Vendor Enquiry confirms this quantity must be produced rather than pulled from ready stock. Same Supplier -> Company fallback as custom_moq.",
			"insert_after": "custom_moq",
		},
		{
			"fieldname": "custom_default_supplier",
			"fieldtype": "Link",
			"label": "Default Supplier",
			"options": "Supplier",
			"description": "BRD D.2 -- who this item is normally sourced from. Settable independently of the item's Series (e.g. re-sourcing just this one item); when unset, catalog.utils.item_default_supplier falls back to the Series' own supplier.",
			"insert_after": "custom_moq",
		},
		{
			"fieldname": "custom_dealer_codes_section",
			"fieldtype": "Section Break",
			"label": "Dealer Codes & Catalog Visibility",
			"insert_after": "custom_moq",
		},
		{
			"fieldname": "custom_dealer_codes",
			"fieldtype": "Table",
			"label": "Dealer Codes",
			"options": "Item Dealer Code",
			"description": "Per-dealer customer item code (BRD D.4). A dealer's catalog only shows items where sample_issued is checked for that dealer (BRD C.1.5).",
			"insert_after": "custom_dealer_codes_section",
		},
		{
			"fieldname": "custom_images_section",
			"fieldtype": "Section Break",
			"label": "Image Gallery",
			"insert_after": "custom_dealer_codes",
		},
		{
			"fieldname": "custom_images",
			"fieldtype": "Table",
			"label": "Images",
			"options": "Product Image",
			"insert_after": "custom_images_section",
		},
	]
}


def setup_catalog():
	create_item_groups()
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)


def create_item_groups():
	root = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ["in", ["", None]]}, "name")
	if not root:
		# ERPNext normally ships "All Item Groups" as the tree root, but a site
		# installed purely via CLI (install-app without an intervening
		# `bench migrate` to sync ERPNext's own fixtures, or before the Setup
		# Wizard ever ran) can reach this point with no Item Group at all --
		# a real deploy hit exactly this (LinkValidationError: Could not find
		# Parent Item Group: All Item Groups). Create the root ourselves
		# rather than hardcoding its name and letting the first child group's
		# insert fail: it's a structural node with no business-decision
		# content (unlike e.g. Company, which this app never invents on the
		# site's behalf -- see warehouse.setup), so there's nothing to get
		# wrong by creating it here.
		root = "All Item Groups"
		if not frappe.db.exists("Item Group", root):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": root, "is_group": 1}).insert(ignore_permissions=True)

	for group in ITEM_GROUPS:
		if frappe.db.exists("Item Group", group):
			continue
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": group,
				"parent_item_group": root,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
