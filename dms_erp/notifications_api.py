"""The staff app's notification bell.

Approval requests (approvals.api), the reorder digest (purchase.reorder_api), overdue
unpaid POs (purchase.po_api), reservation reminders (sales.reservation) and inactive
dealers (sales.dealer_activity) all notify through ERPNext's own Notification Log --
which only ERPNext Desk ever displayed. The staff app's header bell was a static icon
with a permanent red dot, so a Management user working only in the staff app never saw
an approval waiting for them. These two endpoints read and clear the same Notification
Log rows, so Desk and the staff app stay in step (marking one read clears both).

Scoped to frappe.session.user on purpose, never a caller-supplied user: the bell shows
"my" notifications, the same rule Desk's own bell applies.
"""

import frappe
from frappe.utils import cint, strip_html_tags

DEFAULT_LIMIT = 30
MAX_LIMIT = 100


def _serialize(row) -> dict:
	return {
		"id": row.name,
		# Subjects are HTML in Desk (bold dealer names etc.); the staff app renders text.
		"subject": strip_html_tags(row.subject or "").strip(),
		"documentType": row.document_type,
		"documentName": row.document_name,
		"read": bool(row.read),
		"createdAt": row.creation,
	}


@frappe.whitelist(methods=["GET"])
def list_my_notifications(limit: int = DEFAULT_LIMIT) -> dict:
	limit = max(1, min(cint(limit) or DEFAULT_LIMIT, MAX_LIMIT))
	user = frappe.session.user
	rows = frappe.get_all(
		"Notification Log",
		filters={"for_user": user},
		fields=["name", "subject", "document_type", "document_name", "read", "creation"],
		order_by="creation desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	unread = frappe.db.count("Notification Log", {"for_user": user, "read": 0})
	return {"items": [_serialize(r) for r in rows], "unread": unread}


@frappe.whitelist(methods=["POST"])
def mark_notifications_read(ids: list[str] | str | None = None) -> dict:
	"""`ids` omitted = mark every one of this user's notifications read. An id that
	isn't this user's own is silently skipped, never an error that would reveal it
	exists."""
	user = frappe.session.user
	filters = {"for_user": user, "read": 0}
	if ids is not None:
		if isinstance(ids, str):
			ids = frappe.parse_json(ids) if ids.strip().startswith("[") else [ids]
		if not ids:
			return {"unread": frappe.db.count("Notification Log", {"for_user": user, "read": 0})}
		filters["name"] = ["in", list(ids)]
	for name in frappe.get_all("Notification Log", filters=filters, pluck="name", ignore_permissions=True):
		frappe.db.set_value("Notification Log", name, "read", 1, update_modified=False)
	return {"unread": frappe.db.count("Notification Log", {"for_user": user, "read": 0})}
