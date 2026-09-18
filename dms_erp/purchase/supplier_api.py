"""Supplier directory — every other module (Purchase Order.supplier, Item Price
Proposal.supplier, Bay Allocation.supplier, Inward Truck.supplier) already treats
"supplier" as a bare native `Supplier`, with no custom fields added anywhere. This
module adds no doctype: it's the read endpoint that surface was always missing —
list/get over `Supplier`, the same gap `sales.dealer_api` closed for `Customer`.

Unlike Customer, Supplier has no per-company credit-limit child table to net out —
there's genuinely nothing else to resolve here beyond the native fields.

`custom_latitude`/`custom_longitude` (BRD C.1.6) are the one exception: two plain
Custom Fields (purchase/setup.py) for supplier GPS capture, a prerequisite for
route optimisation (BRD C.5) once that's built. `set_supplier_location` is the
only write endpoint this module needs — nothing else on Supplier is edited here.
"""

import frappe
from frappe import _

SUPPLIER_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}


def _assert_can_manage_suppliers():
	if not set(frappe.get_roles(frappe.session.user)) & SUPPLIER_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can update supplier details."), frappe.PermissionError)


def _serialize(
	name: str, supplier_name: str, supplier_group: str | None, country: str | None, disabled: int, latitude=None, longitude=None
) -> dict:
	return {
		"id": name,
		"name": supplier_name,
		"group": supplier_group,
		"country": country,
		"disabled": bool(disabled),
		"latitude": latitude,
		"longitude": longitude,
	}


@frappe.whitelist(methods=["GET"])
def list_suppliers(search: str | None = None, disabled: bool = False):
	filters = {"disabled": ["=", 1 if disabled else 0]}
	if search:
		filters["supplier_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all(
		"Supplier",
		filters=filters,
		fields=["name", "supplier_name", "supplier_group", "country", "disabled", "custom_latitude", "custom_longitude"],
		order_by="supplier_name asc",
	)
	return [
		_serialize(r.name, r.supplier_name, r.supplier_group, r.country, r.disabled, r.custom_latitude, r.custom_longitude) for r in rows
	]


@frappe.whitelist(methods=["GET"])
def get_supplier(supplier: str):
	doc = frappe.get_doc("Supplier", supplier)
	return _serialize(doc.name, doc.supplier_name, doc.supplier_group, doc.country, doc.disabled, doc.custom_latitude, doc.custom_longitude)


@frappe.whitelist(methods=["POST", "PUT"])
def set_supplier_location(supplier: str, latitude: float, longitude: float):
	_assert_can_manage_suppliers()

	doc = frappe.get_doc("Supplier", supplier)
	doc.custom_latitude = latitude
	doc.custom_longitude = longitude
	doc.save(ignore_permissions=True)
	return _serialize(doc.name, doc.supplier_name, doc.supplier_group, doc.country, doc.disabled, doc.custom_latitude, doc.custom_longitude)
