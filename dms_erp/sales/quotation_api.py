"""Quotation Builder (BRD §7.6 retail markup) — ERPNext's native Quotation doctype,
submitted immediately on creation (same one-action pattern as Phase 4's Purchase
Order — the frontend's builder has no separate draft step either). Rates are always
computed server-side from the approved dealer price (never client-supplied), and
every line must be in the dealer's assigned catalog — both are BRD-mandated gates,
not just report-time checks.
"""

import frappe
from frappe import _
from frappe.utils import add_days, today

from dms_erp.catalog.dealer_catalog_api import is_visible
from dms_erp.pricing.api import get_dealer_price
from dms_erp.warehouse.utils import default_company

QUOTATION_WRITE_ROLES = {"Pacific Sales", "Pacific Management", "System Manager"}


def _assert_can_manage_quotations():
	if not set(frappe.get_roles(frappe.session.user)) & QUOTATION_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage quotations."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"number": doc.name,
		"date": doc.transaction_date,
		"dealerId": doc.party_name,
		"validTill": doc.valid_till,
		"markupPct": doc.custom_markup_pct,
		"freight": doc.custom_freight,
		"inquiryId": doc.custom_inquiry,
		"lines": [{"itemCode": row.item_code, "qty": row.qty, "rate": row.rate} for row in doc.items],
		"total": doc.grand_total,
	}


@frappe.whitelist(methods=["GET"])
def list_quotations(dealer: str | None = None):
	filters = {"quotation_to": "Customer"}
	if dealer:
		filters["party_name"] = dealer
	names = frappe.get_all("Quotation", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Quotation", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def get_quotation(quotation: str):
	return _serialize(frappe.get_doc("Quotation", quotation))


@frappe.whitelist(methods=["POST"])
def create_quotation(
	dealer: str,
	lines: list[dict],
	markup_pct: float,
	freight: float = 0,
	validity_days: int = 7,
	inquiry: str | None = None,
):
	_assert_can_manage_quotations()

	if not lines:
		frappe.throw(_("At least one line is required."), frappe.ValidationError)

	items = []
	for line in lines:
		item = line["item"]
		if not is_visible(dealer, item):
			frappe.throw(_("{0} is not in this dealer's assigned catalog.").format(item), frappe.PermissionError)
		dealer_price = get_dealer_price(item)
		if dealer_price is None:
			frappe.throw(_("{0} has no approved dealer price yet.").format(item), frappe.ValidationError)
		rate = round(dealer_price * (1 + float(markup_pct) / 100))
		items.append({"item_code": item, "qty": line["qty"], "rate": rate})

	doc = frappe.get_doc(
		{
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": dealer,
			"company": default_company(),
			"transaction_date": today(),
			"valid_till": add_days(today(), int(validity_days)),
			"custom_markup_pct": markup_pct,
			"custom_freight": freight,
			"custom_inquiry": inquiry,
			"items": items,
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()

	if inquiry:
		frappe.db.set_value("Inquiry", inquiry, "status", "Quoted")

	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def convert_to_order(quotation: str, expected_dispatch=None):
	from erpnext.selling.doctype.quotation.quotation import make_sales_order

	from dms_erp.sales.order_api import finalize_new_order

	_assert_can_manage_quotations()

	qtn = frappe.get_doc("Quotation", quotation)
	so = make_sales_order(quotation)
	if expected_dispatch:
		so.delivery_date = expected_dispatch
		for row in so.items:
			row.delivery_date = expected_dispatch

	order = finalize_new_order(so, source_type="Quotation", source_ref=quotation)

	if qtn.custom_inquiry:
		frappe.db.set_value("Inquiry", qtn.custom_inquiry, "status", "Converted to Order")

	return order
