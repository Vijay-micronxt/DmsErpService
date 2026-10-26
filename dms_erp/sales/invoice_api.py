"""Sales Invoice read API -- every submitted invoice, not only the ones raised from an
app order (order_api only ever shows invoices linked to a Sales Order, which the
Tally-migrated history never is).

No per-invoice paid/overdue status is returned: for migrated invoices ERPNext's own
status and outstanding_amount are unreliable (receipts were mostly posted as journals
never allocated against an invoice -- see finance.ledger). What a dealer actually owes
is the ledger balance, served by dealer_api's `outstanding`. Receipts that genuinely
were allocated against an invoice are listed on its detail.

The tally_* columns only exist where the Tally migration ran, so they're read
per-site through `_has` rather than assumed.
"""

import frappe
from frappe import _

from dms_erp.catalog.utils import item_weight_per_box_kg
from dms_erp.pagination import clamp

INVOICE_READ_ROLES = {"DMS Sales", "DMS Management", "DMS Finance", "System Manager"}
INVOICE_TYPES = ["Sales", "Credit Note", "Opening", "Proforma"]

_SEARCH_FIELDS = ["name", "customer", "customer_name", "tally_voucher_no", "tally_vehicle_no", "tally_lr_no", "tally_eway_bill_no"]
_TRANSPORT_FIELDS = {
	"transporter": "tally_transporter",
	"vehicleNo": "tally_vehicle_no",
	"lrNo": "tally_lr_no",
	"lrDate": "tally_lr_date",
	"ewayBillNo": "tally_eway_bill_no",
	"irn": "tally_irn",
}


def _assert_can_read_invoices():
	if not set(frappe.get_roles(frappe.session.user)) & INVOICE_READ_ROLES:
		frappe.throw(_("Only Sales or Management can view invoices."), frappe.PermissionError)


def _has(field: str) -> bool:
	return field == "name" or frappe.get_meta("Sales Invoice").has_field(field)


def _invoice_type(row) -> str:
	if row.get("is_return"):
		return "Credit Note"
	if row.get("is_opening") == "Yes":
		return "Opening"
	if "proforma" in (row.get("tally_voucher_type") or "").lower():
		return "Proforma"
	return "Sales"


def _type_condition(invoice_type: str) -> str:
	proforma = "lower(ifnull(tally_voucher_type, '')) like '%%proforma%%'" if _has("tally_voucher_type") else "0"
	return {
		"Credit Note": "is_return = 1",
		"Opening": "is_return = 0 and is_opening = 'Yes'",
		"Proforma": f"is_return = 0 and is_opening != 'Yes' and {proforma}",
		"Sales": f"is_return = 0 and is_opening != 'Yes' and not ({proforma})",
	}[invoice_type]


def _serialize_row(row) -> dict:
	return {
		"id": row.name,
		"date": row.posting_date,
		"dealer": row.customer,
		"dealerName": row.customer_name,
		"type": _invoice_type(row),
		"netTotal": row.net_total,
		"taxTotal": row.total_taxes_and_charges,
		"grandTotal": row.grand_total,
		"vehicleNo": row.get("tally_vehicle_no"),
		"lrNo": row.get("tally_lr_no"),
	}


@frappe.whitelist(methods=["GET"])
def list_sales_invoices(
	search: str | None = None,
	dealer: str | None = None,
	invoice_type: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	_assert_can_read_invoices()
	limit, offset = clamp(limit, offset)
	if invoice_type and invoice_type not in INVOICE_TYPES:
		frappe.throw(_("Invalid invoice_type: {0}").format(invoice_type), frappe.ValidationError)

	conditions = ["docstatus = 1"]
	params: dict = {}
	if dealer:
		conditions.append("customer = %(dealer)s")
		params["dealer"] = dealer
	if from_date:
		conditions.append("posting_date >= %(from_date)s")
		params["from_date"] = from_date
	if to_date:
		conditions.append("posting_date <= %(to_date)s")
		params["to_date"] = to_date
	if invoice_type:
		conditions.append(f"({_type_condition(invoice_type)})")
	if search:
		fields = [f for f in _SEARCH_FIELDS if _has(f)]
		conditions.append("(" + " or ".join(f"`{f}` like %(search)s" for f in fields) + ")")
		params["search"] = f"%{search.strip()}%"
	where = " and ".join(conditions)

	optional = [f for f in ("tally_voucher_type", "tally_vehicle_no", "tally_lr_no") if _has(f)]
	columns = ", ".join(
		["name", "posting_date", "customer", "customer_name", "is_return", "is_opening", "net_total", "total_taxes_and_charges", "grand_total", *optional]
	)
	total = frappe.db.sql(f"select count(*) from `tabSales Invoice` where {where}", params)[0][0]
	rows = frappe.db.sql(
		f"""select {columns} from `tabSales Invoice` where {where}
		order by posting_date desc, name desc limit %(limit)s offset %(offset)s""",
		{**params, "limit": limit, "offset": offset},
		as_dict=True,
	)
	return {"items": [_serialize_row(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@frappe.whitelist(methods=["GET"])
def get_sales_invoice(invoice: str):
	_assert_can_read_invoices()
	return _invoice_detail(frappe.get_doc("Sales Invoice", invoice))


def _line_weight_kg(row) -> float | None:
	"""BRD C.1.3 -- weight on the invoice. stock_qty is the line in the item's stock UOM
	(boxes), whatever UOM it was billed in; the batch's own weight wins over the item's
	standard one, as at allocation. None (not 0) when no weight is known, so a total is
	never shown as smaller than it really is."""
	per_box = None
	if row.get("batch_no"):
		per_box = frappe.db.get_value("Batch", row.batch_no, "custom_batch_weight_kg") or None
	per_box = per_box or item_weight_per_box_kg(row.item_code) or None
	qty = row.get("stock_qty") or row.qty
	return round(per_box * qty, 2) if per_box and qty else None


def _invoice_detail(doc) -> dict:
	"""Unguarded core of get_sales_invoice -- also what the dealer portal's own
	get_my_invoice/list_my_invoices return (ownership is that endpoint's check, in
	place of INVOICE_READ_ROLES). Nothing here is staff-internal: it's the invoice
	document itself plus the receipts allocated against it."""
	receipts = frappe.db.sql(
		"""
		select pe.name, pe.posting_date, pe.mode_of_payment, pe.reference_no, per.allocated_amount
		from `tabPayment Entry Reference` per join `tabPayment Entry` pe on pe.name = per.parent
		where per.reference_doctype = 'Sales Invoice' and per.reference_name = %s and pe.docstatus = 1
		order by pe.posting_date
		""",
		(doc.name,),
		as_dict=True,
	)
	line_weights = [_line_weight_kg(row) for row in doc.items]
	return {
		**_serialize_row(doc),
		"dueDate": doc.due_date,
		# Only when every line's weight is known -- a partial sum would understate it.
		"totalWeightKg": round(sum(line_weights), 2) if line_weights and None not in line_weights else None,
		# Customer.gstin only exists where India Compliance is installed.
		"dealerGstin": doc.get("billing_address_gstin")
		or (
			frappe.db.get_value("Customer", doc.customer, "gstin")
			if frappe.get_meta("Customer").has_field("gstin")
			else None
		),
		"address": doc.get("address_display"),
		"poNumber": doc.po_no,
		"returnAgainst": doc.return_against,
		"roundingAdjustment": doc.rounding_adjustment,
		"remarks": doc.remarks,
		"transport": {key: doc.get(field) for key, field in _TRANSPORT_FIELDS.items() if _has(field)},
		"items": [
			{
				"itemCode": row.item_code,
				"itemName": row.item_name,
				"qty": row.qty,
				"uom": row.uom,
				"rate": row.rate,
				"amount": row.amount,
				"batchNo": row.get("batch_no"),
				"weightKg": weight,
			}
			for row, weight in zip(doc.items, line_weights)
		],
		"taxes": [{"description": row.description, "rate": row.rate, "amount": row.tax_amount} for row in doc.taxes],
		"receipts": [
			{
				"id": r.name,
				"date": r.posting_date,
				"mode": r.mode_of_payment,
				"referenceNo": r.reference_no,
				"amount": r.allocated_amount,
			}
			for r in receipts
		],
	}
