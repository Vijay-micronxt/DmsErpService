"""Dealer directory — every other module (Inquiry.dealer, Quotation.party_name,
Sales Order.customer, Dealer Catalog, WhatsApp Message) already treats "dealer" as
a bare native `Customer` (see dashboard/api.py's credit-exposure alert), with no
custom fields added anywhere. This module adds no doctype and no custom field: it's
the read endpoint that surface was always missing — list/get over `Customer`.
"""

import frappe


def _serialize(name: str, customer_name: str, customer_group: str | None, territory: str | None, credit_limit: float | None, disabled: int) -> dict:
	return {
		"id": name,
		"name": customer_name,
		"group": customer_group,
		"territory": territory,
		"creditLimit": credit_limit or 0,
		"disabled": bool(disabled),
	}


@frappe.whitelist(methods=["GET"])
def list_dealers(search: str | None = None, disabled: bool = False):
	filters = {"disabled": ["=", 1 if disabled else 0]}
	if search:
		filters["customer_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all(
		"Customer",
		filters=filters,
		fields=["name", "customer_name", "customer_group", "territory", "credit_limit", "disabled"],
		order_by="customer_name asc",
	)
	return [_serialize(r.name, r.customer_name, r.customer_group, r.territory, r.credit_limit, r.disabled) for r in rows]


@frappe.whitelist(methods=["GET"])
def get_dealer(dealer: str):
	doc = frappe.get_doc("Customer", dealer)
	return _serialize(doc.name, doc.customer_name, doc.customer_group, doc.territory, doc.credit_limit, doc.disabled)
