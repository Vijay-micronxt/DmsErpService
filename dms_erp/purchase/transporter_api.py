"""Transporter & Vehicle Master (BRD C.1.7) — the Morbi (Gujarat) -> Bengaluru
full-load fleet, mastered for future pickup/route planning. ERPNext's own
transporter concept (a checkbox flag on Supplier) doesn't model vehicle-type
capacity, so this is a genuine custom master: Transporter (parent) + a
Vehicle child table, following the same shape as `Pickup Run`.

`vehicle_type` here (BRD: "e.g. Truck / Container") is intentionally its own
plain field, not a Link to the existing `Vehicle Type` doctype -- that one is
a different concept (box capacity for Pickup Run's warehouse-pickup capacity
check), not the tonnage/mode fleet this module masters.

Out of scope for this phase, per BRD C.5/C.5.1 and explicit instruction: no
Pickup/Route Plan, route optimisation, GPS/live tracking, or driver
assignment flow -- those are future consumers of this master, not built
here. driver_name/driver_mobile on the Vehicle row are optional free-text
(BRD: "assigned on a pickup plan", and no Driver master exists anywhere in
this app -- same precedent as Supplier.custom_insurance_holder).

Two assumptions made where the BRD (including Part G's own open-points
list) doesn't say, flagged here rather than silently decided:
- Vehicle Number is treated as unique across every transporter's fleet
  (enforced in this API layer, not the child-table JSON, since Frappe's
  own `unique` flag isn't reliable on child-table fields) -- a real
  registration number is unique in reality, so a collision here is almost
  certainly a data-entry mistake worth catching, not a case to allow.
- Standard Rate Basis is modeled as Currency (a rate value), not a
  free-text description of the rate method.
"""

import frappe
from frappe import _

from dms_erp.pagination import clamp
from dms_erp.phone_utils import clean_indian_mobile

TRANSPORTER_WRITE_ROLES = {"DMS Purchase", "System Manager"}


def _assert_can_manage_transporters():
	if not set(frappe.get_roles(frappe.session.user)) & TRANSPORTER_WRITE_ROLES:
		frappe.throw(_("Only Purchase can manage the transporter & vehicle master."), frappe.PermissionError)


def _serialize_vehicle(row) -> dict:
	return {
		"id": row.name,
		"ownerName": row.owner_name,
		"vehicleNumber": row.vehicle_number,
		"vehicleType": row.vehicle_type,
		"mode": row.mode,
		"capacityTonnes": row.capacity_tonnes,
		"transitDays": row.transit_days,
		"standardRateBasis": row.standard_rate_basis,
		"driverName": row.driver_name,
		"driverMobile": row.driver_mobile,
	}


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"transporterName": doc.transporter_name,
		"contact": doc.contact,
		"vehicles": [_serialize_vehicle(row) for row in doc.vehicles],
	}


def _clean_vehicle_number(vehicle_number: str) -> str:
	clean = (vehicle_number or "").strip().upper()
	if not clean:
		frappe.throw(_("Vehicle Number is required."), frappe.ValidationError)
	return clean


def _assert_vehicle_number_available(vehicle_number: str, exclude_row_name: str | None = None):
	"""Vehicle Number is unique across every transporter's fleet, not just within
	one transporter -- see this module's own docstring for why."""
	existing = frappe.get_all("Transporter Vehicle", filters={"vehicle_number": vehicle_number}, fields=["name", "parent"])
	for row in existing:
		if row.name == exclude_row_name:
			continue
		frappe.throw(
			_("Vehicle {0} is already registered under transporter {1}.").format(vehicle_number, row.parent),
			frappe.ValidationError,
		)


def _clean_driver_mobile(driver_mobile: str | None) -> str | None:
	if not driver_mobile:
		return None
	clean = clean_indian_mobile(driver_mobile)
	if not clean:
		frappe.throw(_("{0} is not a valid 10-digit Indian mobile number.").format(driver_mobile), frappe.ValidationError)
	return clean


def _validate_vehicle(vehicle: dict, exclude_row_name: str | None = None) -> dict:
	"""Normalizes and validates one vehicle dict, returning it ready to append/set
	onto a Transporter's `vehicles` child table. Required-field and Select-option
	checks (vehicle_number, mode) are left to Frappe's own doc.insert()/.save()
	validation, same as every other doctype in this app -- only what Frappe can't
	express on its own (capacity > 0, cross-parent vehicle-number uniqueness,
	mobile format) is checked here."""
	vehicle_number = _clean_vehicle_number(vehicle.get("vehicle_number"))
	_assert_vehicle_number_available(vehicle_number, exclude_row_name=exclude_row_name)

	capacity_tonnes = vehicle.get("capacity_tonnes")
	if capacity_tonnes is not None and float(capacity_tonnes) <= 0:
		frappe.throw(_("Capacity (Tonnes) must be greater than 0."), frappe.ValidationError)

	transit_days = vehicle.get("transit_days")
	if transit_days is not None and int(transit_days) < 0:
		frappe.throw(_("Transit Days can't be negative."), frappe.ValidationError)

	return {
		"owner_name": vehicle.get("owner_name"),
		"vehicle_number": vehicle_number,
		"vehicle_type": vehicle.get("vehicle_type"),
		"mode": vehicle.get("mode"),
		"capacity_tonnes": capacity_tonnes,
		"transit_days": transit_days,
		"standard_rate_basis": vehicle.get("standard_rate_basis"),
		"driver_name": vehicle.get("driver_name"),
		"driver_mobile": _clean_driver_mobile(vehicle.get("driver_mobile")),
	}


@frappe.whitelist(methods=["GET"])
def list_transporters(search: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = {"transporter_name": ["like", f"%{search}%"]} if search else {}
	total = frappe.db.count("Transporter", filters=filters)
	names = frappe.get_all(
		"Transporter", filters=filters, pluck="name", order_by="transporter_name", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Transporter", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_transporter(transporter: str):
	return _serialize(frappe.get_doc("Transporter", transporter))


@frappe.whitelist(methods=["POST"])
def create_transporter(transporter_name: str, contact: str | None = None, vehicles: list[dict] | None = None):
	_assert_can_manage_transporters()

	if not transporter_name or not transporter_name.strip():
		frappe.throw(_("Transporter Name is required."), frappe.ValidationError)

	validated = [_validate_vehicle(v) for v in (vehicles or [])]
	# Two vehicles in the same request naming the same plate would both pass the
	# per-vehicle uniqueness check above (neither is saved yet to collide against) --
	# catch that within-request duplicate here too.
	seen = set()
	for v in validated:
		if v["vehicle_number"] in seen:
			frappe.throw(_("Vehicle {0} is listed more than once.").format(v["vehicle_number"]), frappe.ValidationError)
		seen.add(v["vehicle_number"])

	doc = frappe.get_doc(
		{
			"doctype": "Transporter",
			"transporter_name": transporter_name.strip(),
			"contact": contact,
			"vehicles": validated,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_transporter(transporter: str, contact: str | None = None):
	_assert_can_manage_transporters()

	doc = frappe.get_doc("Transporter", transporter)
	doc.contact = contact
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def add_vehicle(transporter: str, vehicle: dict):
	_assert_can_manage_transporters()

	doc = frappe.get_doc("Transporter", transporter)
	doc.append("vehicles", _validate_vehicle(vehicle))
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_vehicle(transporter: str, vehicle_name: str, patch: dict):
	_assert_can_manage_transporters()

	doc = frappe.get_doc("Transporter", transporter)
	row = next((r for r in doc.vehicles if r.name == vehicle_name), None)
	if not row:
		frappe.throw(_("Vehicle {0} not found on {1}.").format(vehicle_name, transporter), frappe.ValidationError)

	current = {
		"owner_name": row.owner_name,
		"vehicle_number": row.vehicle_number,
		"vehicle_type": row.vehicle_type,
		"mode": row.mode,
		"capacity_tonnes": row.capacity_tonnes,
		"transit_days": row.transit_days,
		"standard_rate_basis": row.standard_rate_basis,
		"driver_name": row.driver_name,
		"driver_mobile": row.driver_mobile,
	}
	current.update(patch)
	validated = _validate_vehicle(current, exclude_row_name=vehicle_name)
	row.update(validated)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def remove_vehicle(transporter: str, vehicle_name: str):
	_assert_can_manage_transporters()

	doc = frappe.get_doc("Transporter", transporter)
	doc.vehicles = [r for r in doc.vehicles if r.name != vehicle_name]
	doc.save(ignore_permissions=True)
	return _serialize(doc)
