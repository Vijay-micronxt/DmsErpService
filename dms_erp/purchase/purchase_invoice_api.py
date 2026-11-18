"""Purchase Invoices for the staff app: list + detail, read-only.

This app never posts a Purchase Invoice (see po_api's note on Paid/Unpaid): every one here is a
supplier bill brought over from Tally. So this is only the read side -- find a bill, see what it
billed and how the goods travelled -- without going into the ERPNext desk. No create or edit on
purpose: a posted bill is an accounting record.
"""

import frappe
from frappe import _

from dms_erp.pagination import clamp, date_range_filter

PURCHASE_INVOICE_READ_ROLES = {"DMS Purchase", "DMS Finance", "DMS Management", "System Manager"}

# ERPNext's own payment-state vocabulary for a posted bill, plus Cancelled.
STATUSES = ["Unpaid", "Overdue", "Partly Paid", "Paid", "Return", "Debit Note Issued", "Cancelled"]


def _assert_can_read():
	if not set(frappe.get_roles(frappe.session.user)) & PURCHASE_INVOICE_READ_ROLES:
		frappe.throw(_("You don't have access to Purchase Invoices."), frappe.PermissionError)


def _serialize_row(row) -> dict:
	return {
		"id": row.name,
		"date": row.posting_date,
		"supplier": row.supplier,
		"supplierName": row.supplier_name,
		"billNo": row.bill_no,
		"status": row.status,
		"total": row.grand_total,
		"outstanding": row.outstanding_amount,
	}


@frappe.whitelist(methods=["GET"])
def list_purchase_invoices(
	search: str | None = None,
	supplier: str | None = None,
	status: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	"""One page of posted (and cancelled) purchase invoices. Status, the bill date range and search
	(our number, the supplier's bill number, the supplier, the Tally voucher) are all applied here.
	Drafts never appear: a draft is someone mid-entry in the desk."""
	_assert_can_read()
	limit, offset = clamp(limit, offset)
	if status and status not in STATUSES:
		frappe.throw(_("Invalid status: {0}").format(status), frappe.ValidationError)

	filters: dict = {"docstatus": ["in", [1, 2]]}
	if supplier:
		filters["supplier"] = supplier
	if status == "Cancelled":
		filters["docstatus"] = 2
	elif status:
		filters["status"] = status
	date_range_filter(filters, "posting_date", from_date, to_date)
	if search and search.strip():
		pattern = ["like", f"%{search.strip()}%"]
		matches: set[str] = set()
		for field in ("name", "bill_no", "supplier", "supplier_name", "tally_voucher_no"):
			if frappe.get_meta("Purchase Invoice").has_field(field) or field == "name":
				matches |= set(frappe.get_all("Purchase Invoice", filters={**filters, field: pattern}, pluck="name"))
		filters["name"] = ["in", list(matches) or [""]]

	total = frappe.db.count("Purchase Invoice", filters=filters)
	rows = frappe.get_all(
		"Purchase Invoice",
		filters=filters,
		fields=["name", "posting_date", "supplier", "supplier_name", "bill_no", "status", "grand_total", "outstanding_amount", "docstatus"],
		order_by="posting_date desc, name desc",
		limit_start=offset,
		limit_page_length=limit,
	)
	items = []
	for r in rows:
		item = _serialize_row(r)
		if r.docstatus == 2:
			item["status"] = "Cancelled"
		items.append(item)
	return {"items": items, "total": total, "limit": limit, "offset": offset}


@frappe.whitelist(methods=["GET"])
def get_purchase_invoice(purchase_invoice: str):
	_assert_can_read()
	doc = frappe.get_doc("Purchase Invoice", purchase_invoice)
	if doc.docstatus == 0:
		frappe.throw(_("{0} is a draft, not a posted Purchase Invoice.").format(purchase_invoice), frappe.DoesNotExistError)

	def tally(field):
		return doc.get(field) or None

	return {
		**_serialize_row(doc),
		"status": "Cancelled" if doc.docstatus == 2 else doc.status,
		"billDate": doc.bill_date,
		"dueDate": doc.due_date,
		"netTotal": doc.net_total,
		"taxTotal": doc.total_taxes_and_charges,
		"paid": (doc.grand_total or 0) - (doc.outstanding_amount or 0),
		"purchaseOrders": sorted({r.purchase_order for r in doc.items if r.get("purchase_order")}),
		"purchaseReceipts": sorted({r.purchase_receipt for r in doc.items if r.get("purchase_receipt")}),
		"voucherNo": tally("tally_voucher_no"),
		"transporter": tally("tally_transporter"),
		"vehicleNo": tally("tally_vehicle_no"),
		"lrNo": tally("tally_lr_no"),
		"ewayBillNo": tally("tally_eway_bill_no"),
		"taxes": [
			{"description": t.description or t.account_head, "amount": t.tax_amount} for t in doc.taxes or []
		],
		"items": [
			{
				"itemCode": r.item_code,
				"itemName": r.item_name,
				"hsn": r.get("gst_hsn_code"),
				"qty": r.qty,
				"uom": r.uom,
				"rate": r.rate,
				"amount": r.amount,
			}
			for r in doc.items
		],
	}
