"""Dealer directory — every other module (Inquiry.dealer, Quotation.party_name,
Sales Order.customer, Dealer Catalog, WhatsApp Message) already treats "dealer" as
a bare native `Customer` (see dashboard/api.py's credit-exposure alert). This
module adds no doctype: it's the read endpoint that surface was always missing —
list/get over `Customer`.

Credit limit lives on the Customer Credit Limit child table (keyed by company), not
a flat Customer.credit_limit column — see dashboard/api.py::_credit_exposure_alerts
for why. Resolved here against this app's own default_company(), same as everywhere
else that needs "which company does this app's data belong to".

`classification` (Standard Dealer/Dealer/Master Dealer, BRD C.1.4) and `dealerType`
(Retail/Bulk/Project, BRD C.4.3) are the two Customer custom fields sales/setup.py
adds — see pricing.dealer_classification and sales.order_channel for what reads
and writes them. Both are read-only here; classification is recomputed nightly and
dealerType is edited directly on the Customer, not through this module.
"""

import frappe

from dms_erp.warehouse.utils import default_company


def _serialize(
	name: str,
	customer_name: str,
	customer_group: str | None,
	territory: str | None,
	credit_limit: float | None,
	disabled: int,
	classification: str | None = None,
	dealer_type: str | None = None,
) -> dict:
	return {
		"id": name,
		"name": customer_name,
		"group": customer_group,
		"territory": territory,
		"creditLimit": credit_limit or 0,
		"disabled": bool(disabled),
		"classification": classification,
		"dealerType": dealer_type,
	}


def _credit_limits_for(customer_names: list[str], company: str) -> dict[str, float]:
	if not customer_names:
		return {}
	rows = frappe.get_all(
		"Customer Credit Limit",
		filters={"parent": ["in", customer_names], "company": company},
		fields=["parent", "credit_limit"],
	)
	return {r.parent: r.credit_limit for r in rows}


@frappe.whitelist(methods=["GET"])
def list_dealers(search: str | None = None, disabled: bool = False):
	filters = {"disabled": ["=", 1 if disabled else 0]}
	if search:
		filters["customer_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all(
		"Customer",
		filters=filters,
		fields=["name", "customer_name", "customer_group", "territory", "disabled", "custom_dealer_classification", "custom_dealer_type"],
		order_by="customer_name asc",
	)
	credit_limits = _credit_limits_for([r.name for r in rows], default_company())
	return [
		_serialize(
			r.name,
			r.customer_name,
			r.customer_group,
			r.territory,
			credit_limits.get(r.name),
			r.disabled,
			r.custom_dealer_classification,
			r.custom_dealer_type,
		)
		for r in rows
	]


@frappe.whitelist(methods=["GET"])
def get_dealer(dealer: str):
	doc = frappe.get_doc("Customer", dealer)
	credit_limit = frappe.db.get_value("Customer Credit Limit", {"parent": dealer, "company": default_company()}, "credit_limit")
	return _serialize(
		doc.name,
		doc.customer_name,
		doc.customer_group,
		doc.territory,
		credit_limit,
		doc.disabled,
		doc.custom_dealer_classification,
		doc.custom_dealer_type,
	)
