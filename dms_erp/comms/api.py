"""WhatsApp-based dealer interaction (BRD §5, and the dealer-facing half of §20).

The real WhatsApp Business API send/receive integration is middleware-side per the
BRD — this module is only the system of record: it logs the thread, lets staff
trigger an outbound send, and exposes a webhook contract for that (not-yet-built)
middleware to post inbound messages and real delivery receipts back. There's no fake
"Delivered after 1.2s" timeout here like the frontend's UI-only simulation — a real
Sent -> Delivered -> Read progression can only come from the actual transport layer.

WhatsApp Message is a genuine custom doctype. Frappe's own Communication doctype is
the nearest built-in and was considered, but its `status` vocabulary (Open/Closed/
Replied — thread-handling state) doesn't model WhatsApp's delivery-receipt states
(Sent/Delivered/Read/Failed), and "WhatsApp" isn't a stock `communication_medium`
option on a doctype shared by unrelated core features — extending it would mean
customizing a widely-used shared doctype rather than modeling Pacific's own,
differently-shaped lifecycle.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from dms_erp.comms.utils import MESSAGE_TEMPLATES, verify_webhook_secret

COMMS_WRITE_ROLES = {"Pacific Sales", "Pacific Management", "System Manager"}


def _assert_can_manage_comms():
	if not set(frappe.get_roles(frappe.session.user)) & COMMS_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage WhatsApp messages."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"dealerId": doc.dealer,
		"direction": doc.direction,
		"text": doc.text,
		"status": doc.status,
		"relatedType": doc.related_type,
		"relatedRef": doc.related_reference,
		"sentAt": doc.sent_at,
		"sentBy": doc.sent_by,
	}


@frappe.whitelist(methods=["GET"])
def list_messages(dealer: str):
	names = frappe.get_all("WhatsApp Message", filters={"dealer": dealer}, pluck="name", order_by="sent_at asc")
	return [_serialize(frappe.get_doc("WhatsApp Message", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def last_message(dealer: str):
	thread = list_messages(dealer)
	return thread[-1] if thread else None


@frappe.whitelist(methods=["GET"])
def unreplied_inbound_count(dealer: str) -> int:
	thread = list_messages(dealer)
	reversed_thread = list(reversed(thread))
	last_inbound_idx = next((i for i, m in enumerate(reversed_thread) if m["direction"] == "Inbound"), -1)
	if last_inbound_idx == -1:
		return 0
	tail = thread[len(thread) - last_inbound_idx:]
	replied = any(m["direction"] == "Outbound" for m in tail)
	return 0 if replied else 1


@frappe.whitelist(methods=["GET"])
def list_templates():
	return MESSAGE_TEMPLATES


@frappe.whitelist(methods=["POST"])
def send_message(dealer: str, text: str, related_type: str = "General", related_reference: str | None = None):
	_assert_can_manage_comms()

	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"dealer": dealer,
			"direction": "Outbound",
			"text": text,
			"status": "Sent",
			"related_type": related_type,
			"related_reference": related_reference,
			"sent_at": now_datetime(),
			"sent_by": frappe.session.user,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def mark_read(message: str):
	_assert_can_manage_comms()

	doc = frappe.get_doc("WhatsApp Message", message)
	doc.status = "Read"
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook_inbound_message(
	secret: str,
	dealer: str,
	text: str,
	related_type: str = "General",
	related_reference: str | None = None,
	sent_at=None,
):
	"""Called by the WhatsApp middleware when a dealer sends a new message."""
	verify_webhook_secret(secret)

	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"dealer": dealer,
			"direction": "Inbound",
			"text": text,
			"status": "Delivered",  # arrived, not yet marked read by staff — see mark_read
			"related_type": related_type,
			"related_reference": related_reference,
			"sent_at": sent_at or now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook_status_update(secret: str, message: str, status: str):
	"""Called by the WhatsApp middleware with a real delivery receipt for an outbound
	message (Sent -> Delivered -> Read, or Failed)."""
	verify_webhook_secret(secret)

	doc = frappe.get_doc("WhatsApp Message", message)
	if doc.direction != "Outbound":
		frappe.throw(_("Delivery receipts only apply to outbound messages."), frappe.ValidationError)

	doc.status = status
	doc.save(ignore_permissions=True)
	return _serialize(doc)
