"""ISS-2026-00633 -- list/add/edit/delete for Transport Rate Zone, so the rate master
can be kept up from the staff app instead of only from Desk, which staff don't use.

Writes are Management/System Manager only, matching the doctype's own permissions:
a rate here feeds the freight suggestion on every quotation, so it's a pricing
decision, not routine sales data. Sales can read. The rules on what makes a valid
row (6-digit pincodes, no overlapping ranges for one vehicle type, distance for
Per Km) live in the doctype's validate(), so Desk and this API enforce the same
thing.
"""

import frappe
from frappe import _

from dms_erp.pagination import clamp

TRANSPORT_RATE_WRITE_ROLES = {"DMS Management", "System Manager"}
TRANSPORT_RATE_READ_ROLES = TRANSPORT_RATE_WRITE_ROLES | {"DMS Sales"}
_FIELDS = ["name", "zone_name", "from_pincode", "to_pincode", "vehicle_type", "rate_basis", "rate", "distance_km", "notes"]


def _assert_role(roles: set[str], message: str):
	if not set(frappe.get_roles(frappe.session.user)) & roles:
		frappe.throw(message, frappe.PermissionError)


def _serialize(row) -> dict:
	return {
		"id": row.name,
		"zoneName": row.zone_name,
		"fromPincode": row.from_pincode,
		"toPincode": row.to_pincode,
		"vehicleType": row.vehicle_type,
		"rateBasis": row.rate_basis,
		"rate": row.rate,
		"distanceKm": row.distance_km,
		"notes": row.notes,
	}


@frappe.whitelist(methods=["GET"])
def list_transport_rates(search: str | None = None, limit: int = 50, offset: int = 0):
	"""`search` matches a zone name or vehicle type, or -- when it's a 6-digit
	pincode -- the zones whose range covers it, so staff can check "what would
	this pincode get?" the same way the Quotation Builder looks it up."""
	_assert_role(TRANSPORT_RATE_READ_ROLES, _("Only Sales or Management can view transport rates."))
	limit, offset = clamp(limit, offset)
	filters = {}
	search = (search or "").strip()
	if search:
		if search.isdigit() and len(search) == 6:
			filters = {"from_pincode": ["<=", search], "to_pincode": [">=", search]}
		else:
			pattern = ["like", f"%{search}%"]
			matches = set(frappe.get_all("Transport Rate Zone", filters={"zone_name": pattern}, pluck="name")) | set(
				frappe.get_all("Transport Rate Zone", filters={"vehicle_type": pattern}, pluck="name")
			)
			filters = {"name": ["in", list(matches) or [""]]}
	total = frappe.db.count("Transport Rate Zone", filters=filters)
	rows = frappe.get_all(
		"Transport Rate Zone",
		filters=filters,
		fields=_FIELDS,
		order_by="from_pincode asc, vehicle_type asc",
		limit_start=offset,
		limit_page_length=limit,
	)
	return {"items": [_serialize(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@frappe.whitelist(methods=["POST", "PUT"])
def save_transport_rate(
	zone_name: str,
	from_pincode: str,
	rate_basis: str,
	rate: float,
	to_pincode: str | None = None,
	vehicle_type: str | None = None,
	distance_km: float | None = None,
	notes: str | None = None,
	id: str | None = None,
):
	"""Creates a row, or updates `id` when given. The zone name is the row's name
	(autoname field:zone_name), so renaming an existing zone goes through
	frappe.rename_doc rather than silently creating a second row."""
	_assert_role(TRANSPORT_RATE_WRITE_ROLES, _("Only Management can change transport rates."))
	zone_name = (zone_name or "").strip()
	if id:
		if id != zone_name:
			frappe.rename_doc("Transport Rate Zone", id, zone_name, force=True)
		doc = frappe.get_doc("Transport Rate Zone", zone_name)
	else:
		doc = frappe.new_doc("Transport Rate Zone")
	doc.update(
		{
			"zone_name": zone_name,
			"from_pincode": from_pincode,
			"to_pincode": to_pincode or from_pincode,
			"vehicle_type": vehicle_type,
			"rate_basis": rate_basis,
			"rate": rate,
			"distance_km": distance_km,
			"notes": notes,
		}
	)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "DELETE"])
def delete_transport_rate(id: str):
	_assert_role(TRANSPORT_RATE_WRITE_ROLES, _("Only Management can change transport rates."))
	frappe.delete_doc("Transport Rate Zone", id, ignore_permissions=True)
	return {"deleted": id}
