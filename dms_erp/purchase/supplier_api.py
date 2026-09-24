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
only narrow write endpoint; `create_supplier` / `update_supplier` cover the rest of the
master-data write side (BRD MD-02): name, group, country, GPS, disabled.
"""

import frappe
from frappe import _

SUPPLIER_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}


def _assert_can_manage_suppliers():
	if not set(frappe.get_roles(frappe.session.user)) & SUPPLIER_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage suppliers."), frappe.PermissionError)


def _serialize(
	name: str,
	supplier_name: str,
	supplier_group: str | None,
	country: str | None,
	disabled: int,
	latitude=None,
	longitude=None,
	insurance_holder: str | None = None,
) -> dict:
	return {
		"id": name,
		"name": supplier_name,
		"group": supplier_group,
		"country": country,
		"disabled": bool(disabled),
		"latitude": latitude,
		"longitude": longitude,
		# BRD C.1.6 — which insurer a damage claim against this supplier routes to.
		"insuranceHolder": insurance_holder,
	}


@frappe.whitelist(methods=["GET"])
def list_suppliers(search: str | None = None, disabled: bool = False):
	filters = {"disabled": ["=", 1 if disabled else 0]}
	if search:
		filters["supplier_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all(
		"Supplier",
		filters=filters,
		fields=[
			"name",
			"supplier_name",
			"supplier_group",
			"country",
			"disabled",
			"custom_latitude",
			"custom_longitude",
			"custom_insurance_holder",
		],
		order_by="supplier_name asc",
	)
	return [
		_serialize(
			r.name,
			r.supplier_name,
			r.supplier_group,
			r.country,
			r.disabled,
			r.custom_latitude,
			r.custom_longitude,
			r.custom_insurance_holder,
		)
		for r in rows
	]


@frappe.whitelist(methods=["GET"])
def get_supplier(supplier: str):
	doc = frappe.get_doc("Supplier", supplier)
	return _serialize(
		doc.name,
		doc.supplier_name,
		doc.supplier_group,
		doc.country,
		doc.disabled,
		doc.custom_latitude,
		doc.custom_longitude,
		doc.custom_insurance_holder,
	)


@frappe.whitelist(methods=["POST", "PUT"])
def set_supplier_location(supplier: str, latitude: float, longitude: float):
	_assert_can_manage_suppliers()

	doc = frappe.get_doc("Supplier", supplier)
	doc.custom_latitude = latitude
	doc.custom_longitude = longitude
	doc.save(ignore_permissions=True)
	return get_supplier(doc.name)


@frappe.whitelist(methods=["POST"])
def create_supplier(
	name: str,
	group: str | None = None,
	country: str | None = None,
	latitude: float | None = None,
	longitude: float | None = None,
	insurance_holder: str | None = None,
):
	"""BRD MD-02 — create a supplier (a native Supplier). `group` falls back to the site's
	Buying Settings default when omitted."""
	_assert_can_manage_suppliers()

	name = (name or "").strip()
	if not name:
		frappe.throw(_("A supplier name is required."), frappe.ValidationError)
	# A renamed supplier keeps its original id, so the name can be taken as an id even when no supplier is displayed under it.
	if frappe.db.exists("Supplier", name) or frappe.db.exists("Supplier", {"supplier_name": name}):
		frappe.throw(_("A supplier named {0} already exists.").format(name), frappe.DuplicateEntryError)

	values = {"doctype": "Supplier", "supplier_name": name}
	if group:
		values["supplier_group"] = group
	if country:
		values["country"] = country
	if latitude is not None:
		values["custom_latitude"] = latitude
	if longitude is not None:
		values["custom_longitude"] = longitude
	if insurance_holder:
		values["custom_insurance_holder"] = insurance_holder.strip()

	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	return get_supplier(doc.name)


@frappe.whitelist(methods=["POST", "PUT"])
def update_supplier(supplier: str, patch: dict):
	"""Patch keys: name, group, country, latitude, longitude, disabled, insuranceHolder."""
	_assert_can_manage_suppliers()

	field_map = {
		"name": "supplier_name",
		"group": "supplier_group",
		"country": "country",
		"latitude": "custom_latitude",
		"longitude": "custom_longitude",
		"insuranceHolder": "custom_insurance_holder",
	}

	doc = frappe.get_doc("Supplier", supplier)
	if "name" in patch:
		new_name = (patch["name"] or "").strip()
		if not new_name:
			frappe.throw(_("A supplier name is required."), frappe.ValidationError)
		taken = frappe.db.exists("Supplier", {"supplier_name": new_name, "name": ["!=", supplier]}) or (
			new_name != supplier and frappe.db.exists("Supplier", new_name)
		)
		if taken:
			frappe.throw(_("A supplier named {0} already exists.").format(new_name), frappe.DuplicateEntryError)
		patch = {**patch, "name": new_name}
	for key, value in patch.items():
		if key in field_map:
			doc.set(field_map[key], value)
		elif key == "disabled":
			doc.disabled = 1 if value else 0
	doc.save(ignore_permissions=True)
	return get_supplier(doc.name)
