"""MOM 07-Oct-2026 §3.1 -- the 6-month average freight cost per kg by transport type,
shown to staff while they set a quotation's Freight, "to help users set a safe cost
buffer. This is a guide, not a fixed value."

STAFF ONLY, on purpose: it is an internal cost figure, and MOM §3.2 says internal
cost details are hidden from the dealer-facing quotation. Nothing in dealer_portal_api
or the WhatsApp flow reads it, and a dealer session can't reach this module at all
(auth.middleware confines it to dealer_portal_api).

The average is total cost / total kg over the window per transport type -- weighted
by weight -- not a mean of each shipment's own cost-per-kg: a 500 kg van load must not
count the same as a 20-tonne truck. A transport type with no shipments in the window
is simply absent (an honest empty, never a placeholder number), and `shipments` says
how much data a figure rests on, since an average of two entries is a thin guide.

Writes are Management/Purchase (the people who see what freight really cost); Sales,
Warehouse and Management can read the reference.
"""

import frappe
from frappe import _
from frappe.utils import add_months, flt, getdate, today

from dms_erp.pagination import clamp

REFERENCE_MONTHS = 6
TRANSPORT_TYPES = ("Truck", "Container")
FREIGHT_WRITE_ROLES = {"DMS Management", "DMS Purchase", "System Manager"}
FREIGHT_READ_ROLES = FREIGHT_WRITE_ROLES | {"DMS Sales", "DMS Warehouse"}


def _assert_role(roles: set[str], message: str):
	if not set(frappe.get_roles(frappe.session.user)) & roles:
		frappe.throw(message, frappe.PermissionError)


def _serialize(row) -> dict:
	return {
		"id": row.name,
		"date": row.entry_date,
		"transportType": row.transport_type,
		"weightKg": row.weight_kg,
		"freightCost": row.freight_cost,
		"costPerKg": round(flt(row.freight_cost) / flt(row.weight_kg), 2) if flt(row.weight_kg) else None,
		"reference": row.reference,
		"notes": row.notes,
	}


@frappe.whitelist(methods=["GET"])
def freight_reference(months: int = REFERENCE_MONTHS) -> dict:
	"""Average freight cost per kg over the last `months` (default 6) by transport
	type, for the Quotation Builder's reference box."""
	_assert_role(FREIGHT_READ_ROLES, _("Only staff can view freight costs."))
	months = max(1, min(int(months), 36))
	since = add_months(getdate(today()), -months)
	rows = frappe.db.sql(
		"""
		select transport_type, count(*) as shipments, sum(weight_kg) as total_kg, sum(freight_cost) as total_cost
		from `tabFreight Cost Entry`
		where entry_date >= %s and weight_kg > 0
		group by transport_type
		""",
		(since,),
		as_dict=True,
	)
	by_type = {r.transport_type: r for r in rows}
	return {
		"months": months,
		"since": since,
		"byType": [
			{
				"transportType": t,
				"costPerKg": round(flt(by_type[t].total_cost) / flt(by_type[t].total_kg), 2),
				"shipments": by_type[t].shipments,
				"totalKg": flt(by_type[t].total_kg),
			}
			for t in TRANSPORT_TYPES
			if t in by_type
		],
	}


@frappe.whitelist(methods=["GET"])
def list_freight_costs(limit: int = 50, offset: int = 0):
	_assert_role(FREIGHT_READ_ROLES, _("Only staff can view freight costs."))
	limit, offset = clamp(limit, offset)
	total = frappe.db.count("Freight Cost Entry")
	names = frappe.get_all(
		"Freight Cost Entry", pluck="name", order_by="entry_date desc, creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Freight Cost Entry", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["POST", "PUT"])
def save_freight_cost(
	transport_type: str,
	weight_kg: float,
	freight_cost: float,
	entry_date=None,
	reference: str | None = None,
	notes: str | None = None,
	id: str | None = None,
):
	"""Creates an entry, or updates `id` when given."""
	_assert_role(FREIGHT_WRITE_ROLES, _("Only Purchase or Management can record freight costs."))
	if transport_type not in TRANSPORT_TYPES:
		frappe.throw(_("Transport type must be one of: {0}").format(", ".join(TRANSPORT_TYPES)), frappe.ValidationError)
	doc = frappe.get_doc("Freight Cost Entry", id) if id else frappe.new_doc("Freight Cost Entry")
	doc.update(
		{
			"entry_date": entry_date or today(),
			"transport_type": transport_type,
			"weight_kg": flt(weight_kg),
			"freight_cost": flt(freight_cost),
			"reference": reference,
			"notes": notes,
		}
	)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "DELETE"])
def delete_freight_cost(id: str):
	_assert_role(FREIGHT_WRITE_ROLES, _("Only Purchase or Management can record freight costs."))
	frappe.delete_doc("Freight Cost Entry", id, ignore_permissions=True)
	return {"deleted": id}
