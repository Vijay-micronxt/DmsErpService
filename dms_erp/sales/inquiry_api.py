"""Inquiries (BRD §4) — the demand-signal log a dealer conversation starts from.
No ERPNext doctype models "logged demand, not yet a sales document, that later
converts to a Quotation/Order or gets marked as missed" with Pacific's exact 10-state
status lifecycle, so this is a genuine custom doctype.

Note: unlike most other frontend api/*.ts modules, pacific-tileflow's `inquiries` are
still static read-only seed data — there's no create/update wired up client-side yet.
This module builds the real, mutable backend the BRD describes; the frontend will
need its own follow-up work to call it.

`convert_to_purchase_requirement` (Phase 12) is the missing piece of that 10-state
lifecycle: nothing ever set an Inquiry to "Mapped to PO" because nothing ever raised
a Purchase Order *from* one. It's a thin wrapper over `purchase.po_api.
create_purchase_order` (which still does the actual work, and still enforces its own
Purchase/Management role gate) — not a parallel "purchase requirement" doctype,
since a requirement here is just a PO with a `custom_source_inquiry` link back.

`create_inquiry` (Phase 14) now enforces the same catalog gate `quotation_api.
create_quotation` always has — dealer-assigned visibility and current sellability —
so a hidden or Pulled Back item is rejected here too, not just at the Quotation
step further down the funnel.

list_inquiries is paginated (`limit`/`offset`) and returns `{"items", "total",
"limit", "offset"}`, not a bare list -- reports need the whole result set, so
they call list_all_inquiries (unpaginated, internal-only) instead.
"""

import frappe
from frappe import _

from dms_erp.catalog.dealer_catalog_api import is_visible
from dms_erp.catalog.utils import is_sellable
from dms_erp.pagination import clamp

INQUIRY_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
PURCHASE_REQUIREMENT_STATUSES = {"Open", "Out of Stock", "Pre-order Required"}


def _assert_can_manage_inquiries():
	if not set(frappe.get_roles(frappe.session.user)) & INQUIRY_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage inquiries."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"number": doc.name,
		"date": doc.date,
		"dealerId": doc.dealer,
		"productId": doc.item,
		"qty": doc.qty,
		"status": doc.status,
		"source": doc.source,
		"expectedDelivery": doc.expected_delivery,
		"followUpDate": doc.follow_up_date,
		"assignedTo": doc.assigned_to,
		"remarks": doc.remarks,
		"whatsappReplied": bool(doc.whatsapp_replied),
	}


def _inquiry_filters(dealer: str | None, status: str | None, search: str | None) -> dict:
	filters = {}
	if dealer:
		filters["dealer"] = dealer
	if status:
		filters["status"] = status
	if search:
		filters["item"] = ["like", f"%{search}%"]
	return filters


def list_all_inquiries(dealer: str | None = None, status: str | None = None, search: str | None = None) -> list[dict]:
	"""Unpaginated -- for internal callers (reports) that need the full result set,
	not a page of it. list_inquiries (the whitelisted endpoint) is the paginated one."""
	filters = _inquiry_filters(dealer, status, search)
	names = frappe.get_all("Inquiry", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Inquiry", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_inquiries(
	dealer: str | None = None, status: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0
):
	limit, offset = clamp(limit, offset)
	filters = _inquiry_filters(dealer, status, search)
	total = frappe.db.count("Inquiry", filters=filters)
	names = frappe.get_all(
		"Inquiry", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Inquiry", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_inquiry(inquiry: str):
	return _serialize(frappe.get_doc("Inquiry", inquiry))


@frappe.whitelist(methods=["POST"])
def create_inquiry(
	dealer: str,
	item: str,
	qty: float,
	source: str,
	expected_delivery=None,
	follow_up_date=None,
	assigned_to: str | None = None,
	remarks: str | None = None,
):
	_assert_can_manage_inquiries()

	if not is_visible(dealer, item):
		frappe.throw(_("{0} is not in this dealer's assigned catalog.").format(item), frappe.PermissionError)
	status = frappe.get_cached_value("Item", item, "custom_discontinuation_status") or "Active"
	if not is_sellable(status):
		frappe.throw(_("{0} is {1} and can no longer be quoted.").format(item, status), frappe.ValidationError)

	doc = frappe.get_doc(
		{
			"doctype": "Inquiry",
			"dealer": dealer,
			"item": item,
			"qty": qty,
			"source": source,
			"status": "Open",
			"expected_delivery": expected_delivery,
			"follow_up_date": follow_up_date,
			"assigned_to": assigned_to or frappe.session.user,
			"remarks": remarks,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_inquiry(inquiry: str, patch: dict):
	_assert_can_manage_inquiries()

	field_map = {
		"qty": "qty",
		"status": "status",
		"source": "source",
		"expectedDelivery": "expected_delivery",
		"followUpDate": "follow_up_date",
		"assignedTo": "assigned_to",
		"remarks": "remarks",
		"whatsappReplied": "whatsapp_replied",
	}

	doc = frappe.get_doc("Inquiry", inquiry)
	for key, value in patch.items():
		fieldname = field_map.get(key)
		if fieldname:
			doc.set(fieldname, value)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def convert_to_purchase_requirement(
	inquiry: str,
	supplier: str,
	expected_ready_date,
	ordered_qty: float | None = None,
	remarks: str | None = None,
):
	from dms_erp.purchase.po_api import create_purchase_order

	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.status not in PURCHASE_REQUIREMENT_STATUSES:
		frappe.throw(
			_("Inquiry {0} is {1} — only Open, Out of Stock or Pre-order Required inquiries can become a purchase requirement.").format(
				inquiry, doc.status
			),
			frappe.ValidationError,
		)

	po = create_purchase_order(
		item=doc.item,
		ordered_qty=ordered_qty or doc.qty,
		supplier=supplier,
		expected_ready_date=expected_ready_date,
		remarks=remarks,
		source_inquiry=inquiry,
	)

	frappe.db.set_value("Inquiry", inquiry, "status", "Mapped to PO")
	return po
