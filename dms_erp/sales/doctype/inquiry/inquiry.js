// Copyright (c) 2026, Pacific Inc and contributors
// For license information, please see license.txt

// The staff web app (pacific-tileflow) has always been the only place these
// conversions were reachable from -- Quotation Builder pre-fills from
// ?inquiryId=, and order creation threads inquiry= through. Desk itself never
// got equivalent buttons since this app has no client scripts anywhere else
// (see quotation_api.create_quotation / order_api.create_order for the actual
// whitelisted methods these call -- same ones the web app uses, so a
// Desk-created Quotation/Order is created exactly the same way).

const CLOSED_INQUIRY_STATUSES = ["Converted to Order", "Rejected", "Mapped to PO", "Closed"];

frappe.ui.form.on("Inquiry", {
	refresh(frm) {
		if (frm.is_new() || CLOSED_INQUIRY_STATUSES.includes(frm.doc.status)) {
			return;
		}

		frm.add_custom_button(
			__("Quotation"),
			() => create_quotation_from_inquiry(frm),
			__("Create"),
		);
		frm.add_custom_button(
			__("Order"),
			() => create_order_from_inquiry(frm),
			__("Create"),
		);
	},
});

function create_quotation_from_inquiry(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Create Quotation from {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "markup_pct",
				fieldtype: "Percent",
				label: __("Markup %"),
				default: 0,
				reqd: 1,
			},
			{ fieldname: "freight", fieldtype: "Currency", label: __("Freight"), default: 0 },
			{
				fieldname: "validity_days",
				fieldtype: "Int",
				label: __("Valid For (days)"),
				default: 7,
				reqd: 1,
			},
			{
				fieldname: "taxes_and_charges",
				fieldtype: "Link",
				label: __("Tax Template (GST)"),
				options: "Sales Taxes and Charges Template",
				description: __(
					"Left blank, the quotation is untaxed -- this app never computes GST itself, only an existing template's own rows.",
				),
			},
		],
		primary_action_label: __("Create"),
		primary_action(values) {
			dialog.hide();
			frappe.call({
				method: "dms_erp.sales.quotation_api.create_quotation",
				args: {
					dealer: frm.doc.dealer,
					lines: [{ item: frm.doc.item, qty: frm.doc.qty }],
					markup_pct: values.markup_pct,
					freight: values.freight,
					validity_days: values.validity_days,
					inquiries: [frm.doc.name],
					taxes_and_charges: values.taxes_and_charges || undefined,
				},
				freeze: true,
				freeze_message: __("Creating quotation..."),
				callback(r) {
					if (r.message) {
						frappe.set_route("Form", "Quotation", r.message.id);
					}
				},
			});
		},
	});
	dialog.show();
}

function create_order_from_inquiry(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Create Order from {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "expected_dispatch",
				fieldtype: "Date",
				label: __("Expected Dispatch"),
				reqd: 1,
			},
			{
				fieldname: "customer_po",
				fieldtype: "Data",
				label: __("Dealer's PO Number"),
			},
			{
				fieldname: "taxes_and_charges",
				fieldtype: "Link",
				label: __("Tax Template (GST)"),
				options: "Sales Taxes and Charges Template",
				description: __(
					"Left blank, the order is untaxed -- this app never computes GST itself, only an existing template's own rows.",
				),
			},
		],
		primary_action_label: __("Create"),
		primary_action(values) {
			dialog.hide();
			frappe.call({
				method: "dms_erp.sales.order_api.create_order",
				args: {
					dealer: frm.doc.dealer,
					lines: [{ item: frm.doc.item, qty: frm.doc.qty }],
					expected_dispatch: values.expected_dispatch,
					inquiry: frm.doc.name,
					customer_po: values.customer_po || undefined,
					taxes_and_charges: values.taxes_and_charges || undefined,
				},
				freeze: true,
				freeze_message: __("Creating order..."),
				callback(r) {
					if (r.message) {
						frappe.set_route("Form", "Sales Order", r.message.id);
					}
				},
			});
		},
	});
	dialog.show();
}
