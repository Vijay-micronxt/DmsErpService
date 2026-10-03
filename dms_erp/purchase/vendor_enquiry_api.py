"""Vendor Enquiry (BRD C.4.2) — "A vendor enquiry determines whether material
is available from ready stock or must be produced, capturing expected
readiness and available quantity." A genuine custom doctype: nothing in
ERPNext models "asked a supplier, awaiting/recording their answer" as its
own auditable record. Before this, the only proxy was Purchase Order
Item.custom_ready_qty — a single number staff typed in once the supplier
confirmed, with no record of the enquiry itself (when it was asked, what
was asked, ready-stock-vs-must-produce).

Raising a Purchase Order from a responded enquiry (po_api.create_purchase_order's
own `vendor_enquiry` param) closes this record's own status lifecycle
(Open -> Responded -> Closed) and sets `purchase_order`, the same "real link
back, not just a remarks note" pattern sales.inquiry_api's own
linked_sales_order/linked_quotation already established.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from dms_erp.pagination import clamp

VENDOR_ENQUIRY_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}
AVAILABILITY_OPTIONS = ["Ready Stock", "Must Produce", "Partial"]


def _assert_can_manage_vendor_enquiries():
	if not set(frappe.get_roles(frappe.session.user)) & VENDOR_ENQUIRY_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage vendor enquiries."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"item": doc.item,
		"supplier": doc.supplier,
		"qtyRequired": doc.qty_required,
		"askedOn": doc.asked_on,
		"status": doc.status,
		"remarks": doc.remarks,
		"availability": doc.availability,
		"readyQty": doc.ready_qty,
		"expectedReadinessDate": doc.expected_readiness_date,
		"respondedBy": doc.responded_by,
		"respondedAt": doc.responded_at,
		"responseNotes": doc.response_notes,
		"purchaseOrder": doc.purchase_order,
	}


def _filters(status: str | None, supplier: str | None, item: str | None) -> dict:
	filters = {}
	if status:
		filters["status"] = status
	if supplier:
		filters["supplier"] = supplier
	if item:
		filters["item"] = item
	return filters


def list_all_vendor_enquiries(status: str | None = None) -> list[dict]:
	"""Unpaginated — for internal callers (reorder screen cross-reference,
	reports) that need the full result set. list_vendor_enquiries is the
	paginated, whitelisted one."""
	names = frappe.get_all("Vendor Enquiry", filters=_filters(status, None, None), pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Vendor Enquiry", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_vendor_enquiries(
	status: str | None = None,
	supplier: str | None = None,
	item: str | None = None,
	search: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	limit, offset = clamp(limit, offset)
	filters = _filters(status, supplier, item)
	if search:
		filters["name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Vendor Enquiry", filters=filters)
	names = frappe.get_all(
		"Vendor Enquiry", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Vendor Enquiry", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_vendor_enquiry(enquiry: str):
	return _serialize(frappe.get_doc("Vendor Enquiry", enquiry))


@frappe.whitelist(methods=["POST"])
def create_vendor_enquiry(item: str, supplier: str, qty_required: float, remarks: str | None = None):
	_assert_can_manage_vendor_enquiries()

	if qty_required <= 0:
		frappe.throw(_("qty_required must be greater than 0."), frappe.ValidationError)

	doc = frappe.get_doc(
		{
			"doctype": "Vendor Enquiry",
			"item": item,
			"supplier": supplier,
			"qty_required": qty_required,
			"asked_on": today(),
			"status": "Open",
			"remarks": remarks,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def record_vendor_response(
	enquiry: str,
	availability: str,
	ready_qty: float = 0,
	expected_readiness_date=None,
	response_notes: str | None = None,
):
	"""BRD C.4.2's actual answer: ready stock, must produce, or some of each.
	A Partial response without a ready_qty > 0 is almost certainly a
	mis-entry (if nothing's ready, that's "Must Produce", not "Partial") --
	guarded here rather than silently accepted."""
	_assert_can_manage_vendor_enquiries()

	if availability not in AVAILABILITY_OPTIONS:
		frappe.throw(_("Invalid availability: {0}").format(availability), frappe.ValidationError)
	if availability == "Partial" and not ready_qty:
		frappe.throw(_("Partial availability needs a ready_qty greater than 0."), frappe.ValidationError)

	doc = frappe.get_doc("Vendor Enquiry", enquiry)
	if doc.status == "Closed":
		frappe.throw(_("{0} is already closed (a PO was raised from it).").format(enquiry), frappe.ValidationError)

	doc.availability = availability
	doc.ready_qty = ready_qty if availability != "Must Produce" else 0
	doc.expected_readiness_date = expected_readiness_date
	doc.response_notes = response_notes
	doc.status = "Responded"
	doc.responded_by = frappe.session.user
	doc.responded_at = now_datetime()
	doc.save(ignore_permissions=True)
	return _serialize(doc)
