"""Idempotent install/migrate-time setup for the warehouse module.

Per the architecture decision, bays are ERPNext Warehouses (nested under one of the
two physical warehouses), not a custom stock model — so Bin/Stock Ledger Entry stay
the single source of truth for on-hand quantities everywhere in this module. Only
the attributes ERPNext's Warehouse doctype has no equivalent for (bay type,
dimensions, capacity, suitable categories, zone/row, a 3-state bay status) become
Custom Fields, mirroring how catalog/setup.py extended Item in Phase 2.

Stock Entry (native, used for Phase 3 transfers) similarly only gets custom fields
for the handful of Pacific-specific attributes (transfer type/reason, damage type,
claim ref) it has no native equivalent for.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from dms_erp.catalog.setup import ITEM_GROUPS

# Matches pacific-tileflow's warehouses[] in bay-master.ts.
PHYSICAL_WAREHOUSES = ["Pacific Main — Morbi", "Pacific Buffer — Wankaner"]

BAY_TYPES = ["main", "buffer", "damage", "insurance_claim", "display", "blocked"]
BAY_DIMENSIONS = ["36x6", "36x8", "32x6", "32x8"]
BAY_STATUSES = ["active", "blocked", "reserved"]

TRANSFER_TYPES = [
	"Main→Buffer",
	"Buffer→Main",
	"Main→Damage",
	"Damage→Insurance Claim",
	"Display→Main",
	"Warehouse→Dealer Display",
]
TRANSFER_REASONS = ["Reallocation", "Damage Identified", "Insurance Claim", "Display Setup", "Consolidation", "Other"]

CUSTOM_FIELDS = {
	"Warehouse": [
		{
			"fieldname": "dms_bay_section",
			"fieldtype": "Section Break",
			"label": "DMS Bay Attributes",
			"insert_after": "warehouse_type",
		},
		{"fieldname": "custom_bay_code", "fieldtype": "Data", "label": "Bay Code", "unique": 1, "insert_after": "dms_bay_section"},
		{"fieldname": "custom_bay_type", "fieldtype": "Select", "label": "Bay Type", "options": "\n".join(BAY_TYPES), "insert_after": "custom_bay_code"},
		{"fieldname": "custom_bay_status", "fieldtype": "Select", "label": "Bay Status", "options": "\n".join(BAY_STATUSES), "default": "active", "insert_after": "custom_bay_type"},
		{"fieldname": "dms_bay_column_break", "fieldtype": "Column Break", "insert_after": "custom_bay_status"},
		{"fieldname": "custom_dimensions", "fieldtype": "Select", "label": "Dimensions", "options": "\n".join(BAY_DIMENSIONS), "insert_after": "dms_bay_column_break"},
		{"fieldname": "custom_capacity_boxes", "fieldtype": "Int", "label": "Capacity (Boxes)", "insert_after": "custom_dimensions"},
		{"fieldname": "custom_zone", "fieldtype": "Data", "label": "Zone", "insert_after": "custom_capacity_boxes"},
		{"fieldname": "custom_row", "fieldtype": "Data", "label": "Row", "insert_after": "custom_zone"},
		{
			"fieldname": "custom_suitable_categories",
			"fieldtype": "Small Text",
			"label": "Suitable Categories",
			"description": "Comma-separated Item Group names this bay is designated for. Empty = any category.",
			"insert_after": "custom_row",
		},
	],
	"Stock Entry": [
		{
			"fieldname": "dms_transfer_section",
			"fieldtype": "Section Break",
			"label": "DMS Bay Transfer",
			"insert_after": "purpose",
			"depends_on": "eval:doc.purpose=='Material Transfer'",
		},
		{"fieldname": "custom_transfer_type", "fieldtype": "Select", "label": "Transfer Type", "options": "\n".join(TRANSFER_TYPES), "insert_after": "dms_transfer_section"},
		{"fieldname": "custom_transfer_reason", "fieldtype": "Select", "label": "Transfer Reason", "options": "\n".join(TRANSFER_REASONS), "insert_after": "custom_transfer_type"},
		{"fieldname": "dms_transfer_column_break", "fieldtype": "Column Break", "insert_after": "custom_transfer_reason"},
		{"fieldname": "custom_damage_type", "fieldtype": "Data", "label": "Damage Type", "insert_after": "dms_transfer_column_break"},
		{"fieldname": "custom_claim_ref", "fieldtype": "Data", "label": "Claim Reference", "insert_after": "custom_damage_type"},
		{"fieldname": "custom_remarks", "fieldtype": "Small Text", "label": "Remarks", "insert_after": "custom_claim_ref"},
	],
}


def setup_warehouse():
	create_physical_warehouses()
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	ensure_items_are_batch_tracked()


def create_physical_warehouses():
	company = frappe.defaults.get_global_default("company")
	if not company:
		frappe.log_error(
			title="dms_erp: warehouse setup skipped",
			message="No default Company is configured yet — skipping physical warehouse creation. "
			"Run dms_erp.warehouse.setup.setup_warehouse() again once a Company exists.",
		)
		return

	for name in PHYSICAL_WAREHOUSES:
		if frappe.db.exists("Warehouse", {"warehouse_name": name, "company": company}):
			continue
		frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": name,
				"company": company,
				"is_group": 1,
			}
		).insert(ignore_permissions=True)


def ensure_items_are_batch_tracked():
	"""Every product Pacific sells is batch-tracked (BayLot always carries a batch
	number) — a correction discovered while building Phase 3 on top of Phase 2's
	Item Master. Only touches items in the Pacific-seeded Item Groups so it can't
	silently flip batch-tracking on for unrelated items on an existing site."""
	items = frappe.get_all("Item", filters={"item_group": ["in", ITEM_GROUPS], "has_batch_no": 0}, pluck="name")
	for item in items:
		frappe.db.set_value("Item", item, "has_batch_no", 1)
