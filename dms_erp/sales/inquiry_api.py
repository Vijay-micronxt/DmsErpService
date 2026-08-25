"""Inquiries (BRD §4) — the demand-signal log a dealer conversation starts from.
No ERPNext doctype models "logged demand, not yet a sales document, that later
converts to a Quotation/Order or gets marked as missed" with Pacific's exact 10-state
status lifecycle, so this is a genuine custom doctype.

Note: unlike most other frontend api/*.ts modules, pacific-tileflow's `inquiries` are
still static read-only seed data — there's no create/update wired up client-side yet.
This module builds the real, mutable backend the BRD describes; the frontend will
need its own follow-up work to call it.
"""

import frappe
from frappe import _

INQUIRY_WRITE_ROLES = {"Pacific Sales", "Pacific Management", "System Manager"}


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


@frappe.whitelist(methods=["GET"])
def list_inquiries(dealer: str | None = None, status: str | None = None):
	filters = {}
	if dealer:
		filters["dealer"] = dealer
	if status:
		filters["status"] = status
	names = frappe.get_all("Inquiry", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Inquiry", name)) for name in names]


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
