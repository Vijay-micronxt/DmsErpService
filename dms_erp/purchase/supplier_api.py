"""Supplier directory — every other module (Purchase Order.supplier, Item Price
Proposal.supplier, Bay Allocation.supplier, Inward Truck.supplier) already treats
"supplier" as a bare native `Supplier`, with no custom fields added anywhere. This
module adds no doctype and no custom field: it's the read endpoint that surface was
always missing — list/get over `Supplier`, the same gap `sales.dealer_api` closed
for `Customer`.

Unlike Customer, Supplier has no per-company credit-limit child table to net out —
there's genuinely nothing else to resolve here beyond the native fields.
"""

import frappe


def _serialize(name: str, supplier_name: str, supplier_group: str | None, country: str | None, disabled: int) -> dict:
	return {
		"id": name,
		"name": supplier_name,
		"group": supplier_group,
		"country": country,
		"disabled": bool(disabled),
	}


@frappe.whitelist(methods=["GET"])
def list_suppliers(search: str | None = None, disabled: bool = False):
	filters = {"disabled": ["=", 1 if disabled else 0]}
	if search:
		filters["supplier_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all(
		"Supplier",
		filters=filters,
		fields=["name", "supplier_name", "supplier_group", "country", "disabled"],
		order_by="supplier_name asc",
	)
	return [_serialize(r.name, r.supplier_name, r.supplier_group, r.country, r.disabled) for r in rows]


@frappe.whitelist(methods=["GET"])
def get_supplier(supplier: str):
	doc = frappe.get_doc("Supplier", supplier)
	return _serialize(doc.name, doc.supplier_name, doc.supplier_group, doc.country, doc.disabled)
