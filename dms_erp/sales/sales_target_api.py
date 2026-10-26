"""Salesperson monthly targets (BRD C.12.3 follow-up) — `dealer_api.py` and
`sales/setup.py`'s `custom_salesperson` field description both flagged this as a
deliberate gap: `custom_salesperson` only ever recorded dealer *ownership*, never a
target or performance-vs-target figure. This module is that follow-up.

One `DMS Sales Target` row per salesperson per calendar month (set by Management
only — this is a target being handed down, not something a salesperson sets for
themselves). `salesperson_target_progress` is the actual report: for every
salesperson with a target in the given month, it sums the submitted Sales Order
value for every dealer `custom_salesperson` assigns to them (same dealer-ownership
rule and same Sales Order source `reports.sales_reports.salesperson_assignment_report`
already uses), scoped to that one month, against their target.

Deliberately simple: one flat ₹ target against *all* of a salesperson's dealer
order value (no per-channel split, no dealer-level sub-targets) — nothing in the
BRD or sheet asks for more than that."""

import frappe
from frappe import _
from frappe.utils import get_first_day, get_last_day, getdate, today

from dms_erp.sales import dealer_api

TARGET_WRITE_ROLES = {"DMS Management", "System Manager"}


def _assert_can_manage_targets():
	if not set(frappe.get_roles(frappe.session.user)) & TARGET_WRITE_ROLES:
		frappe.throw(_("Only Management can set salesperson targets."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"salesperson": doc.salesperson,
		"month": str(doc.target_month),
		"targetAmount": doc.target_amount,
	}


@frappe.whitelist(methods=["POST", "PUT"])
def set_salesperson_target(salesperson: str, month, target_amount: float) -> dict:
	"""Upsert — one target per (salesperson, month). `month` can be any date within
	the target month; it's normalized to that month's first day so a second call
	for the same month always updates the same row instead of creating a duplicate."""
	_assert_can_manage_targets()
	if target_amount < 0:
		frappe.throw(_("Target amount cannot be negative."), frappe.ValidationError)

	month_start = get_first_day(getdate(month))
	existing = frappe.db.get_value("DMS Sales Target", {"salesperson": salesperson, "target_month": month_start})
	if existing:
		doc = frappe.get_doc("DMS Sales Target", existing)
		doc.target_amount = target_amount
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "DMS Sales Target",
				"salesperson": salesperson,
				"target_month": month_start,
				"target_amount": target_amount,
			}
		)
		doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["GET"])
def list_salesperson_targets(month=None) -> list[dict]:
	filters = {}
	if month:
		filters["target_month"] = get_first_day(getdate(month))
	names = frappe.get_all("DMS Sales Target", filters=filters, pluck="name", order_by="target_month desc")
	return [_serialize(frappe.get_doc("DMS Sales Target", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def salesperson_target_progress(month=None) -> list[dict]:
	"""Target vs actual for every salesperson with a target set in the given month
	(default: the current month). A salesperson with no target row for that month
	doesn't appear here at all — there's nothing to measure progress against."""
	month_start = get_first_day(getdate(month) if month else today())
	month_end = get_last_day(month_start)

	targets = frappe.get_all(
		"DMS Sales Target",
		filters={"target_month": month_start},
		fields=["salesperson", "target_amount"],
	)
	if not targets:
		return []

	dealers_by_salesperson: dict[str, list[str]] = {}
	for d in dealer_api.list_dealers():
		if d["salesperson"]:
			dealers_by_salesperson.setdefault(d["salesperson"], []).append(d["id"])

	rows = []
	for t in targets:
		dealer_ids = dealers_by_salesperson.get(t.salesperson, [])
		actual = 0
		if dealer_ids:
			actual = frappe.db.sql(
				"""select coalesce(sum(grand_total), 0) from `tabSales Order`
				where customer in %(dealers)s and docstatus = 1
				and transaction_date between %(start)s and %(end)s""",
				{"dealers": dealer_ids, "start": month_start, "end": month_end},
			)[0][0]
		rows.append(
			{
				"salesperson": t.salesperson,
				"month": str(month_start),
				"targetAmount": t.target_amount,
				"actualValue": actual,
				"achievedPct": round(actual / t.target_amount * 100, 1) if t.target_amount else None,
				"dealerCount": len(dealer_ids),
			}
		)

	rows.sort(key=lambda r: (r["achievedPct"] if r["achievedPct"] is not None else -1), reverse=True)
	return rows
