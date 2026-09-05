"""Unloading labour payment (BRD §16) — one voucher per Inward Truck (Phase 3).

Genuinely Pacific-specific (a cash/UPI/cheque payment voucher to a labour
contractor, not a ledger-based vendor bill), so Unloading Charge is a custom
doctype — no Party (Supplier/Customer) is modeled for the contractor.

`boxes` is read from the linked Inward Truck rather than duplicated, and the
charge amount (boxes x rate) is computed on read, never stored — same pattern as
pricing's landingCost/suggestedPrice.

GL posting on `mark_paid` is optional and config-gated (Phase 14), same pattern
as claims_api.py: status/paid-by/paid-at always update regardless. Only when
`DMS Accounting Settings.post_accounting_entries` is checked does it
additionally post a Payment Entry (see finance/accounting.py), and only once the
required accounts are configured there — never to a guessed account.
"""

import frappe
from frappe import _
from frappe.utils import today

from dms_erp.finance import accounting
from dms_erp.pagination import clamp

CHARGE_WRITE_ROLES = {"DMS Warehouse", "DMS Management", "System Manager"}


def _assert_can_manage_charges():
	if not set(frappe.get_roles(frappe.session.user)) & CHARGE_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage unloading charges."), frappe.PermissionError)


def _serialize(doc) -> dict:
	truck = frappe.get_doc("Inward Truck", doc.inward_truck)
	return {
		"id": doc.name,
		"voucherNumber": doc.name,
		"truckId": doc.inward_truck,
		"lr": truck.lr_number,
		"contractor": doc.contractor,
		"boxes": truck.boxes,
		"ratePerBox": doc.rate_per_box,
		"chargeAmount": truck.boxes * doc.rate_per_box,
		"paymentMode": doc.payment_mode,
		"status": doc.status,
		"recordedAt": doc.recorded_at,
		"recordedBy": doc.recorded_by,
		"paidBy": doc.paid_by,
		"paidAt": doc.paid_at,
		"paymentEntry": doc.payment_entry,
		"remarks": doc.remarks,
	}


def _charge_filters(status: str | None) -> dict:
	return {"status": status} if status else {}


def list_all_charges(status: str | None = None) -> list[dict]:
	"""Unpaginated — for internal callers (reports) that need the full result set,
	not a page of it. list_charges (the whitelisted endpoint) is the paginated one."""
	filters = _charge_filters(status)
	names = frappe.get_all("Unloading Charge", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Unloading Charge", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_charges(status: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = _charge_filters(status)
	total = frappe.db.count("Unloading Charge", filters=filters)
	names = frappe.get_all(
		"Unloading Charge", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Unloading Charge", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_charge_for_truck(inward_truck: str):
	name = frappe.db.get_value("Unloading Charge", {"inward_truck": inward_truck}, "name")
	return _serialize(frappe.get_doc("Unloading Charge", name)) if name else None


@frappe.whitelist(methods=["POST"])
def record_charge(inward_truck: str, contractor: str, rate_per_box: float, payment_mode: str, remarks: str | None = None):
	_assert_can_manage_charges()

	if frappe.db.exists("Unloading Charge", {"inward_truck": inward_truck}):
		frappe.throw(_("A charge has already been recorded for this truck."), frappe.DuplicateEntryError)

	doc = frappe.get_doc(
		{
			"doctype": "Unloading Charge",
			"inward_truck": inward_truck,
			"contractor": contractor,
			"rate_per_box": rate_per_box,
			"payment_mode": payment_mode,
			"status": "Pending",
			"recorded_at": today(),
			"recorded_by": frappe.session.user,
			"remarks": remarks,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def mark_paid(charge: str):
	_assert_can_manage_charges()

	doc = frappe.get_doc("Unloading Charge", charge)
	truck = frappe.get_doc("Inward Truck", doc.inward_truck)

	doc.status = "Paid"
	doc.paid_by = frappe.session.user
	doc.paid_at = today()
	doc.payment_entry = accounting.post_unloading_payment(truck.boxes * doc.rate_per_box, doc.name)
	doc.save(ignore_permissions=True)
	return _serialize(doc)
