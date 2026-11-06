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
	// 6 Nov 2026 -- matches ERPNext's own Opportunity -> Quotation exactly: no
	// dialog, just open_mapped_doc routing straight to a new, unsaved,
	// pre-filled Quotation form (inquiry_api.make_quotation does the mapping,
	// including defaulting the address pair that used to be this function's own
	// dialog-first fix for the gap-sheet's #1/#8). Staff edit Markup %/Freight/
	// Tax Template etc. directly on that real form, then click Desk's own
	// native Save/Submit -- see quotation_api.guard_native_submit for the
	// catalog/stock/address checks that create_quotation normally front-loads
	// instead happening at that Submit.
	frappe.model.open_mapped_doc({
		method: "dms_erp.sales.inquiry_api.make_quotation",
		frm: frm,
	});
}

function create_order_from_inquiry(frm) {
	// 6 Nov 2026 -- same native mapped-doc fix as create_quotation_from_inquiry
	// above, same ask: no dialog, open_mapped_doc straight to a new, unsaved,
	// pre-filled Sales Order form (inquiry_api.make_order). See order_api.
	// guard_native_submit for the address/discount/channel checks create_order
	// normally front-loads instead happening at Submit, and for the
	// fulfillment-stage bookkeeping finalize_new_order normally stamps at
	// creation that this draft never went through.
	frappe.model.open_mapped_doc({
		method: "dms_erp.sales.inquiry_api.make_order",
		frm: frm,
	});
}
