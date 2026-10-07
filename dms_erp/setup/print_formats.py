"""Client requirement 8.3 -- "Print formats need to be created, we have the option
of print but the format is blank." Purchase Order and Sales Order are ERPNext's own
native doctypes; printing them from the desk falls back to Frappe's generic default
layout (no company letterhead, no sized item table) until a real Print Format is
created for them. Created the same idempotent way as this app's custom fields/roles
(setup_purchase/create_app_roles) -- re-run on every `bench migrate`, upsert by name
rather than a one-time fixture, so an edit to the template here ships on the next
migrate instead of only taking effect on a fresh install.
"""

import frappe

_ITEM_TABLE_CSS = """
	.pacific-print table { width: 100%; border-collapse: collapse; margin-top: 12px; }
	.pacific-print th, .pacific-print td { border: 1px solid #ccc; padding: 6px 8px; font-size: 10pt; }
	.pacific-print th { background: #f4f4f4; text-align: left; }
	.pacific-print .text-right { text-align: right; }
	.pacific-print .header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
	.pacific-print .title { font-size: 16pt; font-weight: 700; }
	.pacific-print .meta td { border: none; padding: 1px 8px 1px 0; font-size: 10pt; }
"""

PRINT_FORMATS = {
	"Pacific Purchase Order": {
		"doc_type": "Purchase Order",
		"module": "Purchase",
		"html": (
			"<div class='pacific-print'>"
			"<div class='header'>"
			"<div><div class='title'>Purchase Order</div>"
			"<table class='meta'>"
			"<tr><td>PO No.</td><td>{{ doc.name }}</td></tr>"
			"<tr><td>Date</td><td>{{ frappe.utils.formatdate(doc.transaction_date) }}</td></tr>"
			"<tr><td>Supplier</td><td>{{ doc.supplier_name or doc.supplier }}</td></tr>"
			"<tr><td>Required By</td><td>{{ frappe.utils.formatdate(doc.schedule_date) }}</td></tr>"
			"</table></div>"
			"<div><table class='meta'>"
			"<tr><td>Company</td><td>{{ doc.company }}</td></tr>"
			"</table></div>"
			"</div>"
			"<table>"
			"<thead><tr><th>#</th><th>Item Code</th><th>Item Name</th>"
			"<th class='text-right'>Qty</th><th class='text-right'>Rate</th><th class='text-right'>Amount</th></tr></thead>"
			"<tbody>"
			"{% for row in doc.items %}"
			"<tr><td>{{ loop.index }}</td><td>{{ row.item_code }}</td><td>{{ row.item_name }}</td>"
			"<td class='text-right'>{{ row.qty }} {{ row.uom }}</td>"
			"<td class='text-right'>{{ frappe.utils.fmt_money(row.rate, currency=doc.currency) }}</td>"
			"<td class='text-right'>{{ frappe.utils.fmt_money(row.amount, currency=doc.currency) }}</td></tr>"
			"{% endfor %}"
			"</tbody>"
			"<tfoot><tr><td colspan='5' class='text-right'><b>Grand Total</b></td>"
			"<td class='text-right'><b>{{ frappe.utils.fmt_money(doc.grand_total, currency=doc.currency) }}</b></td></tr></tfoot>"
			"</table>"
			"{% if doc.custom_remarks %}<p><b>Remarks:</b> {{ doc.custom_remarks }}</p>{% endif %}"
			"</div>"
		),
	},
	"Pacific Sales Order": {
		"doc_type": "Sales Order",
		"module": "Sales",
		"html": (
			"<div class='pacific-print'>"
			"<div class='header'>"
			"<div><div class='title'>Sales Order</div>"
			"<table class='meta'>"
			"<tr><td>Order No.</td><td>{{ doc.name }}</td></tr>"
			"<tr><td>Date</td><td>{{ frappe.utils.formatdate(doc.transaction_date) }}</td></tr>"
			"<tr><td>Dealer</td><td>{{ doc.customer_name or doc.customer }}</td></tr>"
			"<tr><td>Delivery Date</td><td>{{ frappe.utils.formatdate(doc.delivery_date) }}</td></tr>"
			"<tr><td>Channel</td><td>{{ doc.custom_order_channel }}</td></tr>"
			"</table></div>"
			"<div><table class='meta'>"
			"<tr><td>Company</td><td>{{ doc.company }}</td></tr>"
			"</table></div>"
			"</div>"
			"<table>"
			"<thead><tr><th>#</th><th>Item Code</th><th>Item Name</th>"
			"<th class='text-right'>Qty</th><th class='text-right'>Rate</th><th class='text-right'>Amount</th></tr></thead>"
			"<tbody>"
			"{% for row in doc.items %}"
			"<tr><td>{{ loop.index }}</td><td>{{ row.item_code }}</td><td>{{ row.item_name }}</td>"
			"<td class='text-right'>{{ row.qty }} {{ row.uom }}</td>"
			"<td class='text-right'>{{ frappe.utils.fmt_money(row.rate, currency=doc.currency) }}</td>"
			"<td class='text-right'>{{ frappe.utils.fmt_money(row.amount, currency=doc.currency) }}</td></tr>"
			"{% endfor %}"
			"</tbody>"
			"<tfoot><tr><td colspan='5' class='text-right'><b>Grand Total</b></td>"
			"<td class='text-right'><b>{{ frappe.utils.fmt_money(doc.grand_total, currency=doc.currency) }}</b></td></tr></tfoot>"
			"</table>"
			"</div>"
		),
	},
}


def setup_print_formats():
	for name, spec in PRINT_FORMATS.items():
		if frappe.db.exists("Print Format", name):
			frappe.db.set_value(
				"Print Format", name, {"html": spec["html"], "css": _ITEM_TABLE_CSS, "disabled": 0}, update_modified=False
			)
			continue
		frappe.get_doc(
			{
				"doctype": "Print Format",
				"name": name,
				"doc_type": spec["doc_type"],
				"module": spec["module"],
				"print_format_type": "Jinja",
				"standard": "No",
				"disabled": 0,
				"html": spec["html"],
				"css": _ITEM_TABLE_CSS,
			}
		).insert(ignore_permissions=True)
