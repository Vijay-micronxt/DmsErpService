"""Labour Attendance & Payment (BRD C.9.2) — labourers tracked directly, beyond
consignment-linked Unloading Charge (unloading_api.py): each labourer, days/shifts
present, work done, amount due and amount paid, with payment mode/reference. A
running attendance+dues record per labourer over a period, not per-truck like
Unloading Charge.

`amount_due` is computed, never hand-entered: daily_rate × the count of Present
days in the attendance table, recomputed every time a day is added. Each
attendance day can optionally link back to the Unloading Charge voucher it was
consignment-related work for (BRD's own "links to the unloading vouchers where the
work was consignment-related").

GL posting on `record_payment` is optional and config-gated, same pattern as
claims_api.py and unloading_api.py. `record_payment` can be called more than once
(partial payments accumulate into `amount_paid`); `payment_entry` holds only the
most recent posting's reference, same single-voucher simplification Unloading
Charge already makes — a full payment history table is a bigger ask than this
gap item scopes to.
"""

import frappe
from frappe import _
from frappe.utils import today

from dms_erp.finance import accounting
from dms_erp.pagination import clamp

LABOUR_WRITE_ROLES = {"DMS Warehouse", "DMS Management", "System Manager"}
PAYMENT_MODES = ["Cash", "Bank Transfer", "UPI", "Cheque"]


def _assert_can_manage_labour():
	if not set(frappe.get_roles(frappe.session.user)) & LABOUR_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage labour attendance and payment."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"labourerName": doc.labourer_name,
		"contractor": doc.contractor,
		"dailyRate": doc.daily_rate,
		"periodStart": doc.period_start,
		"periodEnd": doc.period_end,
		"status": doc.status,
		"attendance": [
			{
				"date": row.date,
				"present": bool(row.present),
				"workDone": row.work_done,
				"linkedUnloadingVoucher": row.linked_unloading_voucher,
			}
			for row in sorted(doc.attendance, key=lambda r: r.idx)
		],
		"amountDue": doc.amount_due,
		"amountPaid": doc.amount_paid,
		"paymentMode": doc.payment_mode,
		"paymentReference": doc.payment_reference,
		"paidBy": doc.paid_by,
		"paidAt": doc.paid_at,
		"paymentEntry": doc.payment_entry,
		"remarks": doc.remarks,
	}


def _recompute_amount_due(doc):
	present_days = sum(1 for row in doc.attendance if row.present)
	doc.amount_due = present_days * doc.daily_rate


def _recompute_status(doc):
	if doc.amount_paid <= 0:
		doc.status = "Pending"
	elif doc.amount_paid < doc.amount_due:
		doc.status = "Partially Paid"
	else:
		doc.status = "Paid"


def _record_filters(status: str | None) -> dict:
	return {"status": status} if status else {}


def list_all_labour_records(status: str | None = None) -> list[dict]:
	"""Unpaginated — for internal callers (reports) that need the full result set,
	not a page of it. list_labour_records (the whitelisted endpoint) is the
	paginated one."""
	filters = _record_filters(status)
	names = frappe.get_all("Labour Attendance And Payment", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Labour Attendance And Payment", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_labour_records(status: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = _record_filters(status)
	if search:
		filters["labourer_name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Labour Attendance And Payment", filters=filters)
	names = frappe.get_all(
		"Labour Attendance And Payment",
		filters=filters,
		pluck="name",
		order_by="creation desc",
		limit_start=offset,
		limit_page_length=limit,
	)
	return {
		"items": [_serialize(frappe.get_doc("Labour Attendance And Payment", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_labour_record(record: str):
	return _serialize(frappe.get_doc("Labour Attendance And Payment", record))


@frappe.whitelist(methods=["POST"])
def create_labour_record(
	labourer_name: str, daily_rate: float, contractor: str | None = None, period_start=None, period_end=None
):
	_assert_can_manage_labour()

	doc = frappe.get_doc(
		{
			"doctype": "Labour Attendance And Payment",
			"labourer_name": labourer_name,
			"contractor": contractor,
			"daily_rate": daily_rate,
			"period_start": period_start,
			"period_end": period_end,
			"status": "Pending",
			"amount_due": 0,
			"amount_paid": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def add_attendance_day(
	record: str, date, present: bool = True, work_done: str | None = None, linked_unloading_voucher: str | None = None
):
	_assert_can_manage_labour()

	doc = frappe.get_doc("Labour Attendance And Payment", record)
	doc.append(
		"attendance", {"date": date, "present": 1 if present else 0, "work_done": work_done, "linked_unloading_voucher": linked_unloading_voucher}
	)
	_recompute_amount_due(doc)
	_recompute_status(doc)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def record_payment(record: str, amount_paid: float, payment_mode: str, payment_reference: str | None = None):
	_assert_can_manage_labour()

	if payment_mode not in PAYMENT_MODES:
		frappe.throw(_("Invalid payment_mode: {0}").format(payment_mode), frappe.ValidationError)

	doc = frappe.get_doc("Labour Attendance And Payment", record)
	doc.amount_paid = (doc.amount_paid or 0) + amount_paid
	doc.payment_mode = payment_mode
	doc.payment_reference = payment_reference
	doc.paid_by = frappe.session.user
	doc.paid_at = today()
	_recompute_status(doc)
	if doc.status in ("Partially Paid", "Paid"):
		posted = accounting.post_labour_payment(amount_paid, doc.name)
		doc.payment_entry = posted or doc.payment_entry
	doc.save(ignore_permissions=True)
	return _serialize(doc)
