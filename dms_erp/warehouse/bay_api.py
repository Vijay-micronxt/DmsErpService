"""Bay master (BRD §10) — bays are ERPNext Warehouses (see warehouse/setup.py and
warehouse/utils.py for the mapping and serialization).
"""

import frappe
from frappe import _

from dms_erp.warehouse.utils import CAPACITY_FOR, default_company, get_bay, serialize_bay

BAY_WRITE_ROLES = {"DMS Warehouse", "DMS Management", "System Manager"}


def _assert_can_manage_bays():
	if not set(frappe.get_roles(frappe.session.user)) & BAY_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage bays."), frappe.PermissionError)


@frappe.whitelist(methods=["GET"])
def list_bays():
	names = frappe.get_all("Warehouse", filters={"is_group": 0, "custom_bay_code": ["!=", ""]}, pluck="name")
	return [serialize_bay(frappe.get_doc("Warehouse", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def get_bay_detail(code: str):
	return serialize_bay(get_bay(code))


@frappe.whitelist(methods=["POST"])
def create_bay(
	code: str,
	bay_type: str,
	dimensions: str,
	parent_warehouse: str,
	zone: str,
	row: str,
	suitable_categories: list[str] | None = None,
	capacity_boxes: int | None = None,
	status: str = "active",
):
	_assert_can_manage_bays()

	if frappe.db.exists("Warehouse", {"custom_bay_code": code}):
		frappe.throw(_("Bay {0} already exists.").format(code), frappe.DuplicateEntryError)

	bay = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": f"{bay_type.title()} Bay {code}",
			"company": default_company(),
			"parent_warehouse": parent_warehouse,
			"is_group": 0,
			"custom_bay_code": code,
			"custom_bay_type": bay_type,
			"custom_dimensions": dimensions,
			"custom_capacity_boxes": capacity_boxes or CAPACITY_FOR.get(dimensions, 0),
			"custom_zone": zone,
			"custom_row": row,
			"custom_bay_status": "blocked" if bay_type == "blocked" else status,
			"custom_suitable_categories": ", ".join(suitable_categories or []),
		}
	)
	bay.insert(ignore_permissions=True)
	return serialize_bay(bay)


@frappe.whitelist(methods=["POST"])
def create_bay_grid(
	prefix: str,
	count: int,
	start_at: int,
	bay_type: str,
	dimensions: str,
	parent_warehouse: str,
	zone: str,
	categories: list[str] | None = None,
):
	_assert_can_manage_bays()

	created = 0
	for i in range(int(count)):
		n = str(int(start_at) + i).zfill(2)
		code = f"{prefix}-{n}"
		if frappe.db.exists("Warehouse", {"custom_bay_code": code}):
			continue
		create_bay(
			code=code,
			bay_type=bay_type,
			dimensions=dimensions,
			parent_warehouse=parent_warehouse,
			zone=zone,
			row=f"Row-{i + 1}",
			suitable_categories=categories,
		)
		created += 1
	return {"created": created}


@frappe.whitelist(methods=["POST", "PUT"])
def update_bay(code: str, patch: dict):
	_assert_can_manage_bays()

	field_map = {
		"name": "warehouse_name",
		"type": "custom_bay_type",
		"dimensions": "custom_dimensions",
		"capacityBoxes": "custom_capacity_boxes",
		"status": "custom_bay_status",
		"zone": "custom_zone",
		"row": "custom_row",
		"warehouse": "parent_warehouse",
	}

	bay = get_bay(code)
	for key, value in patch.items():
		if key == "suitableCategories":
			bay.custom_suitable_categories = ", ".join(value or [])
			continue
		fieldname = field_map.get(key)
		if fieldname:
			bay.set(fieldname, value)
	bay.save(ignore_permissions=True)
	return serialize_bay(bay)


@frappe.whitelist(methods=["POST", "DELETE"])
def delete_bay(code: str):
	_assert_can_manage_bays()
	bay = get_bay(code)
	# frappe.delete_doc already refuses a Warehouse with existing stock/ledger
	# entries (LinkExistsError) — no need to duplicate that check here.
	frappe.delete_doc("Warehouse", bay.name, ignore_permissions=True)
	return {"success": True}
