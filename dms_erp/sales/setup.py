"""Quotation and Sales Order are ERPNext's own native doctypes — no custom doctype
needed for either. Only the handful of Pacific-specific attributes they have no
equivalent for become Custom Fields:

- Quotation: the markup % applied over the approved dealer price (BRD §7.6 retail
  markup), and freight (kept as a plain field rather than wired into Sales Taxes and
  Charges, since that needs a GL account this app can't assume exists on every site).
- Sales Order: a warehouse-fulfillment stage (Confirmed -> Picking -> Ready to
  Dispatch -> Dispatched -> Delivered/Cancelled) that's a distinct, manually-advanced
  operational flow layered on top of — not derived from — ERPNext's own
  delivery/billing status, plus a structured stage-history log (no equivalent to a
  queryable, typed timeline in core Frappe) and a vehicle/source-document reference.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

ORDER_STAGES = ["Confirmed", "Picking", "Ready to Dispatch", "Dispatched", "Delivered", "Cancelled"]
ORDER_EVENT_STAGES = ["Created"] + ORDER_STAGES
ORDER_SOURCE_TYPES = ["Inquiry", "Quotation"]

CUSTOM_FIELDS = {
	"Quotation": [
		{
			"fieldname": "pacific_quotation_section",
			"fieldtype": "Section Break",
			"label": "Pacific Pricing",
			"insert_after": "valid_till",
		},
		{"fieldname": "custom_markup_pct", "fieldtype": "Percent", "label": "Retail Markup %", "insert_after": "pacific_quotation_section"},
		{"fieldname": "custom_freight", "fieldtype": "Currency", "label": "Freight", "insert_after": "custom_markup_pct"},
		{"fieldname": "custom_inquiry", "fieldtype": "Link", "label": "Source Inquiry", "options": "Inquiry", "insert_after": "custom_freight"},
	],
	"Sales Order": [
		{
			"fieldname": "pacific_fulfillment_section",
			"fieldtype": "Section Break",
			"label": "Pacific Fulfillment",
			"insert_after": "delivery_date",
		},
		{
			"fieldname": "custom_fulfillment_stage",
			"fieldtype": "Select",
			"label": "Fulfillment Stage",
			"options": "\n".join(ORDER_STAGES),
			"default": "Confirmed",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"insert_after": "pacific_fulfillment_section",
		},
		{"fieldname": "custom_vehicle", "fieldtype": "Data", "label": "Vehicle", "insert_after": "custom_fulfillment_stage"},
		{"fieldname": "pacific_fulfillment_column_break", "fieldtype": "Column Break", "insert_after": "custom_vehicle"},
		{"fieldname": "custom_source_type", "fieldtype": "Select", "label": "Source Type", "options": "\n".join(ORDER_SOURCE_TYPES), "insert_after": "pacific_fulfillment_column_break"},
		{"fieldname": "custom_source_ref", "fieldtype": "Data", "label": "Source Reference", "insert_after": "custom_source_type"},
		{"fieldname": "custom_stage_history", "fieldtype": "Table", "label": "Stage History", "options": "Order Stage Event", "insert_after": "custom_source_ref"},
	],
}


def setup_sales():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
