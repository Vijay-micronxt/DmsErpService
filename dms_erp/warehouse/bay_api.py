"""Bay master (BRD §10) — bays are ERPNext Warehouses (see warehouse/setup.py and
warehouse/utils.py for the mapping and serialization).

list_bays is paginated (`limit`/`offset`, via dms_erp.pagination.clamp) and
returns `{"items", "total", "limit", "offset"}`, not a bare list -- reports and
the warehouse dashboard need the whole result set, so they call list_all_bays
(unpaginated, internal-only) instead of the whitelisted endpoint.
"""

import frappe
from frappe import _

from dms_erp.pagination import clamp
from dms_erp.warehouse.utils import CAPACITY_FOR, default_company, get_bay, serialize_bay

BAY_WRITE_ROLES = {"DMS Warehouse", "DMS Management", "System Manager"}


def _assert_can_manage_bays():
	if not set(frappe.get_roles(frappe.session.user)) & BAY_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage bays."), frappe.PermissionError)


@frappe.whitelist(methods=["GET"])
def list_warehouse_groups(search: str | None = None):
	# The other half of "bays are ERPNext Warehouses" (see module docstring):
	# create_bay's parent_warehouse wants a group Warehouse's raw `name`, which
	# ERPNext autonames with a company-abbreviation suffix (e.g. "Pacific Main —
	# Morbi - PI") -- different from its clean warehouse_name, and nothing else
	# exposed that raw id. This is that lookup, kept minimal since a bay group
	# has nothing worth serializing beyond id/name (unlike a bay itself).
	#
	# search, no pagination: create_warehouse_group means this list can now grow
	# past the original two, but a physical-site directory stays small (dozens,
	# not the thousands a product/order list can reach) -- same shape as
	# dealer_api.list_dealers/supplier_api.list_suppliers, not the paginated
	# {items, total} list endpoints.
	filters = {"is_group": 1}
	if search:
		filters["warehouse_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all("Warehouse", filters=filters, fields=["name", "warehouse_name"], order_by="warehouse_name asc")
	return [{"id": r.name, "name": r.warehouse_name} for r in rows]


@frappe.whitelist(methods=["POST"])
def create_warehouse_group(name: str):
	# The two physical warehouses (warehouse/setup.py's PHYSICAL_WAREHOUSES) are
	# only the initial seed, not a hard limit -- a new physical site is created
	# through here, the same way a bay is created through create_bay, and its
	# `parent_warehouse` (none, since a group warehouse sits at the root) is why
	# it needs no such param itself.
	_assert_can_manage_bays()

	if frappe.db.exists("Warehouse", {"warehouse_name": name, "company": default_company()}):
		frappe.throw(_("A warehouse named {0} already exists.").format(name), frappe.DuplicateEntryError)

	group = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": name,
			"company": default_company(),
			"is_group": 1,
		}
	)
	group.insert(ignore_permissions=True)
	return {"id": group.name, "name": group.warehouse_name}


def _bay_filters(
	search: str | None, bay_type: str | None = None, status: str | None = None, parent_warehouse: str | None = None
) -> dict:
	filters = {"is_group": 0}
	# search's LIKE pattern already can't match an empty string, so it doubles as
	# the "has a real bay code" exclusion the no-search case needs explicitly.
	filters["custom_bay_code"] = ["like", f"%{search}%"] if search else ["!=", ""]
	if bay_type:
		filters["custom_bay_type"] = bay_type
	if status:
		filters["custom_bay_status"] = status
	if parent_warehouse:
		filters["parent_warehouse"] = parent_warehouse
	return filters


def list_all_bays(
	search: str | None = None, bay_type: str | None = None, status: str | None = None, parent_warehouse: str | None = None
) -> list[dict]:
	"""Unpaginated -- for internal callers (reports, dashboard) that need the full
	result set, not a page of it. list_bays (the whitelisted endpoint) is the
	paginated one."""
	filters = _bay_filters(search, bay_type, status, parent_warehouse)
	names = frappe.get_all("Warehouse", filters=filters, pluck="name")
	return [serialize_bay(frappe.get_doc("Warehouse", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_bays(
	search: str | None = None,
	bay_type: str | None = None,
	status: str | None = None,
	parent_warehouse: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	limit, offset = clamp(limit, offset)
	filters = _bay_filters(search, bay_type, status, parent_warehouse)
	total = frappe.db.count("Warehouse", filters=filters)
	names = frappe.get_all(
		"Warehouse", filters=filters, pluck="name", order_by="custom_bay_code asc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [serialize_bay(frappe.get_doc("Warehouse", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


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
