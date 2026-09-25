// Copyright (c) 2026, Pacific Inc and contributors
// For license information, please see license.txt

const CLOSED_INQUIRY_STATUSES = ["Converted to Order", "Rejected", "Mapped to PO", "Closed"];

frappe.listview_settings["Inquiry"] = {
	onload(listview) {
		listview.page.add_actions_menu_item(
			__("Create Quotation"),
			() => create_quotation_from_selected(listview),
			false,
		);
	},
};

function create_quotation_from_selected(listview) {
	const rows = listview.get_checked_items();
	if (rows.length === 0) {
		frappe.msgprint(__("Select one or more inquiries first."));
		return;
	}

	const closed = rows.filter((r) => CLOSED_INQUIRY_STATUSES.includes(r.status));
	if (closed.length > 0) {
		frappe.msgprint(
			__("{0} is already {1} and can't be quoted again.", [closed[0].name, closed[0].status]),
		);
		return;
	}

	const dealer = rows[0].dealer;
	const mismatched = rows.filter((r) => r.dealer !== dealer);
	if (mismatched.length > 0) {
		frappe.msgprint(
			__(
				"All selected inquiries must be for the same dealer -- {0} is for {1}, not {2}.",
				[mismatched[0].name, mismatched[0].dealer, dealer],
			),
		);
		return;
	}

	const dialog = new frappe.ui.Dialog({
		title: __("Create Quotation from {0} inquiries", [rows.length]),
		fields: [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options: `<p>${__("Dealer")}: <strong>${frappe.utils.escape_html(dealer)}</strong></p><ul>${rows
					.map((r) => `<li>${frappe.utils.escape_html(r.name)} — ${frappe.utils.escape_html(r.item)} × ${r.qty}</li>`)
					.join("")}</ul>`,
			},
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
					dealer,
					lines: rows.map((r) => ({ item: r.item, qty: r.qty })),
					markup_pct: values.markup_pct,
					freight: values.freight,
					validity_days: values.validity_days,
					inquiries: rows.map((r) => r.name),
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
