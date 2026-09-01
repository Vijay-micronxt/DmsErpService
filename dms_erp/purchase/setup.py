"""Purchase Order and Purchase Order Item are ERPNext's own native doctypes — no
custom doctype needed for Phase 4 at all. Only supplier-readiness tracking
(`readyQty`, BRD §13.2) and free-text remarks have no ERPNext equivalent, so those
become Custom Fields, same pattern as every prior phase.

`custom_source_inquiry` (Phase 12) is the same idea applied to
`sales.inquiry_api.convert_to_purchase_requirement`: a PO raised directly from a
dealer's Inquiry needs a real link back to it (closing the Inquiry status
lifecycle's unused "Mapped to PO" state), not just a remarks note.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Purchase Order": [
		{"fieldname": "custom_remarks", "fieldtype": "Small Text", "label": "Remarks", "insert_after": "schedule_date"},
		{
			"fieldname": "custom_source_inquiry",
			"fieldtype": "Link",
			"options": "Inquiry",
			"label": "Source Inquiry",
			"insert_after": "custom_remarks",
		},
	],
	"Purchase Order Item": [
		{
			"fieldname": "custom_ready_qty",
			"fieldtype": "Float",
			"label": "Ready Qty (confirmed at supplier)",
			"description": "Material confirmed ready at supplier/factory — updated manually as the supplier confirms (BRD §13.2).",
			"insert_after": "qty",
			"allow_on_submit": 1,
		},
	],
}


def setup_purchase():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
