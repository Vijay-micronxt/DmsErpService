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
from dms_erp.setup.client_scripts import sync_client_scripts

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
		{
			"fieldname": "custom_color",
			"fieldtype": "Data",
			"label": "Colour",
			"description": "BRD C.1.2 Colour -- the item's own colour name (e.g. Ivory Beige), the one the item name is composed from (series + colour + finish, client requirement 8.5). Was labelled Base Colour; Base Colour is its own field below.",
			"insert_after": "custom_finish",
		},
		{
			"fieldname": "custom_base_colour",
			"fieldtype": "Data",
			"label": "Base Colour",
			"description": "BRD C.1.2 -- \"Colour is captured as both Colour and Base Colour\": the broad colour family (e.g. Beige, White, Grey) for filtering, alongside the item's own Colour.",
			"insert_after": "custom_color",
		},
		{
			"fieldname": "custom_colour_attribute_1",
			"fieldtype": "Data",
			"label": "Colour Attribute 1",
			"description": "ISS-2026-00632 -- a secondary colour descriptor alongside Base Colour (e.g. a shade, tone or accent colour), filterable in the dealer portal catalogue.",
			"insert_after": "custom_base_colour",
		},
		{
			"fieldname": "custom_colour_attribute_2",
			"fieldtype": "Data",
			"label": "Colour Attribute 2",
			"insert_after": "custom_colour_attribute_1",
		},
		{
			"fieldname": "custom_design_attribute_1",
			"fieldtype": "Data",
			"label": "Design Attribute 1",
			"description": "ISS-2026-00632 -- a design/pattern descriptor (e.g. Marble-look, Wood-look, Geometric), filterable in the dealer portal catalogue.",
			"insert_after": "custom_colour_attribute_2",
		},
		{
			"fieldname": "custom_design_attribute_2",
			"fieldtype": "Data",
			"label": "Design Attribute 2",
			"insert_after": "custom_design_attribute_1",
		},
		{"fieldname": "custom_series", "fieldtype": "Data", "label": "Series", "insert_after": "custom_design_attribute_2"},
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
			"fieldname": "custom_height_mm",
			"fieldtype": "Float",
			"label": "Tile Height (mm)",
			"description": "Physical tile dimension. When this, Tile Width and Pieces per Box are all set and Sqft per Box is left blank at creation, Sqft per Box is derived from these rather than typed in separately (see catalog.utils.derive_sqft_per_box) -- so the two can't drift apart.",
			"insert_after": "custom_weight_per_box_kg",
		},
		{"fieldname": "custom_width_mm", "fieldtype": "Float", "label": "Tile Width (mm)", "insert_after": "custom_height_mm"},
		{
			"fieldname": "custom_discontinuation_status",
			"fieldtype": "Select",
			"label": "Discontinuation Status",
			"options": "\n".join(DISCONTINUATION_STATUSES),
			"default": "Active",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"insert_after": "custom_width_mm",
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
			"fieldname": "custom_reorder_frequency",
			"fieldtype": "Select",
			"label": "Reorder Frequency",
			"options": "\nMonthly\nEvery 2 months\nEvery 3 months",
			"description": "BRD C.4.1 item-level reorder frequency (D.2: Series reorder_frequency, \"reorder cadence\") -- copied from the Series when the item is created, editable per item. Shown on the reorder review; it does not change the suggested quantity.",
			"insert_after": "custom_production_moq",
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


# Gap-sheet items #2/#5 and #3/#3.1/#6 (native-Desk audit, 6 Nov 2026).
CLIENT_SCRIPTS = {
	# #2/#5 -- Product Series' own size/finish/sub_finish are free text on
	# purpose (BRD C.1.1 -- not a closed list), so they stay the Autocomplete
	# fieldtype (product_series.json), not Select; this just feeds that
	# Autocomplete real suggestions from series_api's own list_series_sizes/
	# list_series_finishes/list_series_sub_finishes (already built, already
	# used by the React app's own SizePicker/FinishPicker/SubFinishPicker --
	# the native Desk form just never called them), instead of free-typing the
	# whole thing out by hand every time.
	"Product Series": """
frappe.ui.form.on('Product Series', {
	onload: function(frm) {
		frappe.call({method: 'dms_erp.catalog.series_api.list_series_sizes', callback: function(r) {
			frm.set_df_property('size', 'options', r.message || []);
		}});
		frappe.call({method: 'dms_erp.catalog.series_api.list_series_finishes', callback: function(r) {
			frm.set_df_property('finish', 'options', r.message || []);
		}});
		frappe.call({method: 'dms_erp.catalog.series_api.list_series_sub_finishes', callback: function(r) {
			frm.set_df_property('sub_finish', 'options', r.message || []);
		}});
	},
});
""",
	# #3/#3.1 -- "with Series selection, details should get auto fetched": size,
	# finish, pieces/sqft/weight per box and reorder frequency all already live
	# on Product Series (series_api._serialize); an Item linked to one had to
	# have every one of those re-typed by hand. #6 ("reorder frequency should be
	# system generated, not manual") folds in here too -- Product Series already
	# has its own reorder_frequency field (confirmed in series_api.py), so this
	# is the same "stop re-typing what's already on the Series" fix, not a new
	# sales-velocity formula that doesn't exist anywhere in this app yet. Only
	# fires on actually changing the Series (not on every refresh), and only
	# fills a field the Series actually has a value for -- never overwrites an
	# Item's own value with a blank.
	"Item": """
frappe.ui.form.on('Item', {
	custom_series_ref: function(frm) {
		if (!frm.doc.custom_series_ref) return;
		frappe.call({
			method: 'frappe.client.get',
			args: {doctype: 'Product Series', name: frm.doc.custom_series_ref},
			callback: function(r) {
				var s = r.message;
				if (!s) return;
				var map = {
					custom_size: s.size,
					custom_finish: s.finish,
					custom_pieces_per_box: s.pieces_per_box,
					custom_sqft_per_box: s.sqft_per_box,
					custom_weight_per_box_kg: s.weight_per_box_kg,
					custom_reorder_frequency: s.reorder_frequency,
				};
				Object.keys(map).forEach(function(field) {
					if (map[field] !== null && map[field] !== undefined && map[field] !== '') {
						frm.set_value(field, map[field]);
					}
				});
			}
		});
	},
	// Gap #9 -- a whole leftover "Tile Details (Tally)" section (tally_item_section
	// and everything under it) from the original Tally migration, duplicating
	// dms_erp's own real fields above under different names/units (confirmed: 13
	// real Custom Field records, not just stray DB columns). Hidden, not deleted --
	// tally_guid in particular is very likely the matching key sales.tally_sync.py
	// (currently disabled, not yet signed off) will need once that feature turns
	// on, so removing the fields outright would risk breaking it later. Guarded
	// per-field so this no-ops harmlessly on a site that never had a Tally import.
	refresh: function(frm) {
		[
			'tally_item_section', 'tally_guid', 'tally_hsn_code', 'tally_gst_rate',
			'tally_height_mm', 'tally_width_mm', 'tally_item_col', 'tally_pieces_per_box',
			'tally_weight_per_piece', 'tally_hexagon', 'tally_hexagon_size',
			'tally_hexagon_sqft', 'tally_stock_category',
		].forEach(function(field) {
			if (frm.fields_dict[field]) frm.toggle_display(field, false);
		});
	},
});
""",
}


def setup_catalog():
	create_item_groups()
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	sync_client_scripts(CLIENT_SCRIPTS)


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
