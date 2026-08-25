"""Purchase Order and Purchase Order Item are ERPNext's own native doctypes — no
custom doctype needed for Phase 4 at all. Only supplier-readiness tracking
(`readyQty`, BRD §13.2) and free-text remarks have no ERPNext equivalent, so those
become Custom Fields, same pattern as every prior phase.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Purchase Order": [
		{"fieldname": "custom_remarks", "fieldtype": "Small Text", "label": "Remarks", "insert_after": "schedule_date"},
	],
	"Purchase Order Item": [
		{
			"fieldname": "custom_ready_qty",
			"fieldtype": "Float",
			"label": "Ready Qty (confirmed at supplier)",
			"description": "Material confirmed ready at supplier/factory — updated manually as the supplier confirms (BRD §13.2).",
			"insert_after": "qty",
		},
	],
}


def setup_purchase():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
