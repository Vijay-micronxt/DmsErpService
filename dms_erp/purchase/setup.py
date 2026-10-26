"""Purchase Order and Purchase Order Item are ERPNext's own native doctypes — no
custom doctype needed for Phase 4 at all. Only supplier-readiness tracking
(`readyQty`, BRD §13.2) and free-text remarks have no ERPNext equivalent, so those
become Custom Fields, same pattern as every prior phase.

`custom_source_inquiry` (Phase 12) is the same idea applied to
`sales.inquiry_api.convert_to_purchase_requirement`: a PO raised directly from a
dealer's Inquiry needs a real link back to it (closing the Inquiry status
lifecycle's unused "Mapped to PO" state), not just a remarks note.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Purchase Receipt": [
		{
			"fieldname": "custom_supplier_invoice_no",
			"fieldtype": "Data",
			"label": "Supplier Invoice No",
			"description": "BRD C.4.4 -- \"supplier invoice reference\" captured at receipt. ERPNext's native Purchase Receipt has no such field (bill_no/bill_date live on Purchase Invoice only, which this app doesn't post -- see finance.claims_api's own note on the Insurance Claim -> Purchase Receipt link).",
			"insert_after": "supplier_delivery_note",
		},
		{
			"fieldname": "custom_supplier_invoice_date",
			"fieldtype": "Date",
			"label": "Supplier Invoice Date",
			"insert_after": "custom_supplier_invoice_no",
		},
	],
	"Purchase Order": [
		{"fieldname": "custom_remarks", "fieldtype": "Small Text", "label": "Remarks", "insert_after": "schedule_date"},
		{
			"fieldname": "custom_source_inquiry",
			"fieldtype": "Link",
			"options": "Inquiry",
			"label": "Source Inquiry",
			"insert_after": "custom_remarks",
		},
		{
			"fieldname": "custom_vendor_enquiry",
			"fieldtype": "Link",
			"options": "Vendor Enquiry",
			"label": "Vendor Enquiry",
			"description": "BRD C.4.2 -- real link back to the Vendor Enquiry that confirmed readiness for this PO, same pattern as custom_source_inquiry for the Inquiry side.",
			"insert_after": "custom_source_inquiry",
		},
		{
			"fieldname": "custom_reorder_plan",
			"fieldtype": "Link",
			"options": "Reorder Plan",
			"label": "Reorder Plan",
			"description": "BRD C.4.1 -- real link back to the Reorder Plan whose reviewed line produced this PO, same pattern as custom_vendor_enquiry.",
			"insert_after": "custom_vendor_enquiry",
		},
		{
			"fieldname": "custom_drop_ship_sales_order",
			"fieldtype": "Link",
			"options": "Sales Order",
			"label": "Drop-ship Sales Order",
			"description": "BRD C.3.3 -- set when this PO ships straight from the supplier to the end customer and never lands in this app's own warehouse stock. One-directional link (PO -> SO only); the reverse view is order_api._serialize's computed dropShipPurchaseOrders list, not a stored field here. Every query that treats open PO qty as available/bookable warehouse supply (purchase.reorder_api, po_api.list_materials_ready_for_pickup, pickup_run_api, route_plan_api, warehouse.inward_api.add_truck) must exclude or guard POs with this field set.",
			"insert_after": "custom_reorder_plan",
		},
		{
			"fieldname": "custom_customer_billing_address",
			"fieldtype": "Link",
			"options": "Address",
			"read_only": 1,
			"label": "Customer Billing Address (Drop-ship)",
			"description": "BRD C.3.3 -- a one-time snapshot of custom_drop_ship_sales_order's own customer_address at PO-creation time, same snapshot-not-live-link convention as Route Plan Stop's Supplier GPS capture, so a later edit to the Sales Order's address never silently rewrites an already-placed drop-ship PO.",
			"insert_after": "custom_drop_ship_sales_order",
		},
		{
			"fieldname": "custom_customer_shipping_address",
			"fieldtype": "Link",
			"options": "Address",
			"read_only": 1,
			"label": "Customer Shipping Address (Drop-ship)",
			"description": "BRD C.3.3 -- same snapshot as custom_customer_billing_address, for shipping_address_name. This is the address the supplier actually ships to.",
			"insert_after": "custom_customer_billing_address",
		},
		{
			"fieldname": "custom_payment_status",
			"fieldtype": "Select",
			"label": "Payment Status",
			"options": "Unpaid\nPaid",
			"default": "Unpaid",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"description": "Client requirement 8.4 -- this app posts no Purchase Invoice/Payment Entry, so there is no native signal for \"paid.\" Manually set by Purchase/Management; po_api.flag_overdue_unpaid_pos reads it to find POs unpaid 180+ days after transaction_date.",
			"insert_after": "custom_customer_shipping_address",
		},
		{
			"fieldname": "custom_paid_on",
			"fieldtype": "Date",
			"label": "Paid On",
			"depends_on": "eval:doc.custom_payment_status==\"Paid\"",
			"insert_after": "custom_payment_status",
		},
		{
			"fieldname": "custom_return_flagged",
			"fieldtype": "Check",
			"label": "Overdue Return Flagged",
			"default": "0",
			"hidden": 1,
			"description": "Set by po_api.flag_overdue_unpaid_pos once Management has been notified this PO is unpaid 180+ days on -- stops the daily job notifying the same PO again every day. Cleared when payment status is set back to Unpaid via mark_po_paid.",
			"insert_after": "custom_paid_on",
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
	"Supplier": [
		{
			"fieldname": "custom_gps_section",
			"fieldtype": "Section Break",
			"label": "GPS Coordinates",
			"insert_after": "supplier_details",
		},
		{
			"fieldname": "custom_latitude",
			"fieldtype": "Float",
			"label": "Latitude",
			"precision": "6",
			"insert_after": "custom_gps_section",
		},
		{
			"fieldname": "custom_longitude",
			"fieldtype": "Float",
			"label": "Longitude",
			"precision": "6",
			"insert_after": "custom_latitude",
		},
		{
			"fieldname": "custom_insurance_holder",
			"fieldtype": "Data",
			"label": "Insurance Holder / Policy Reference",
			"description": "BRD C.1.6 — identifies which insurer a damage claim against this supplier should route to. Free text, not a Link (Insurer isn't its own master anywhere in this app); captured here only, not yet read by finance.claims_api.",
			"insert_after": "custom_longitude",
		},
		{
			"fieldname": "custom_address",
			"fieldtype": "Small Text",
			"label": "Factory Address",
			"description": "BRD C.1.6 — factory location/address, for pickup stop identification (BRD C.5). Free text rather than a linked Address doctype record, matching this app's other Supplier custom fields.",
			"insert_after": "custom_insurance_holder",
		},
		{
			"fieldname": "custom_contact_person",
			"fieldtype": "Data",
			"label": "Material-Ready Contact",
			"description": "BRD C.1.6 — who to reach for vendor enquiry and material-ready follow-up. Free text (name and/or phone), not a Link to Contact.",
			"insert_after": "custom_address",
		},
		# Client finding 7.3 -- supplier contact details editable from the staff app. Plain
		# fields like Customer.custom_phone/custom_email rather than ERPNext's Contact
		# doctype, which the staff app never manages.
		{
			"fieldname": "custom_phone",
			"fieldtype": "Data",
			"label": "Phone",
			"options": "Phone",
			"insert_after": "custom_contact_person",
		},
		{
			"fieldname": "custom_email",
			"fieldtype": "Data",
			"label": "Email",
			"options": "Email",
			"insert_after": "custom_phone",
		},
		{
			"fieldname": "custom_moq_section",
			"fieldtype": "Section Break",
			"label": "MOQ (Vendor Level)",
			"insert_after": "custom_email",
		},
		{
			"fieldname": "custom_moq",
			"fieldtype": "Int",
			"label": "Stock MOQ (Boxes)",
			"description": "BRD C.4.2 -- this vendor's own stock MOQ tier, between an Item's own custom_moq and DMS Purchase Settings' site-wide default in the resolution order.",
			"insert_after": "custom_moq_section",
		},
		{
			"fieldname": "custom_production_moq",
			"fieldtype": "Int",
			"label": "Production MOQ (Boxes)",
			"description": "BRD C.4.2 -- this vendor's own production MOQ tier, same resolution order as custom_moq.",
			"insert_after": "custom_moq",
		},
	],
}


# BRD C.1.7's confirmed Morbi -> Bengaluru full-load fleet (Phase 1 task 1.7). Box
# capacity is left unset on purpose -- it depends on the tiles' weight, so runs are
# checked by weight against the tonnage instead (pickup_run_api._check_weight_capacity).
BRD_FLEET = [
	("Truck 43.5 T", 43.5, "Road"),
	("Truck 42 T", 42.0, "Road"),
	("Truck 41 T", 41.0, "Road"),
	("Truck 40.5 T", 40.5, "Road"),
	("Truck 35 T", 35.0, "Road"),
	("Truck 31 T", 31.0, "Road"),
	("Truck 25 T", 25.0, "Road"),
	("Container 32.5 T (Rail + Road)", 32.5, "Rail + Road"),
	("Container 31 T (Rail + Road)", 31.0, "Rail + Road"),
	("Container 28 T (Ship + Road)", 28.0, "Ship + Road"),
	("Container 31 T (Ship + Road)", 31.0, "Ship + Road"),
]


def seed_brd_fleet():
	"""Adds any BRD fleet vehicle type that's missing; never changes or removes existing ones."""
	if not frappe.get_meta("Vehicle Type").has_field("capacity_tonnes"):
		return
	for name, tonnes, mode in BRD_FLEET:
		if frappe.db.exists("Vehicle Type", name):
			continue
		frappe.get_doc(
			{"doctype": "Vehicle Type", "vehicle_type_name": name, "capacity_tonnes": tonnes, "mode": mode, "capacity_boxes": 0}
		).insert(ignore_permissions=True)


def setup_purchase():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	seed_brd_fleet()
