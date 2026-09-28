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

`_maybe_auto_reply` (run after every inbound message is logged) is the availability/
price auto-responder: comms.intent.classify_message turns the free text into an
intent + a raw item mention (LLM-based — see that module's own docstring for why not
a keyword dictionary), catalog.api.resolve_item_mention turns that mention into a
real Item (deterministic substring matching, not LLM), and the actual stock figure
is a plain warehouse.utils.total_stock_for_item DB read. Only when every one of those
resolves confidently does a reply go out and an Inquiry get logged (source
"WhatsApp") — anything else (LLM disabled/unsure, no matching item, item not in this
dealer's catalog or no longer sellable) is left inbound-unreplied for a human,
exactly as if this feature didn't exist. Wrapped in its own try/except in the
webhook so a bug or outage in the auto-reply path can never break logging the
inbound message itself, which is this module's actual contract.
"""

import frappe
from frappe import _
from frappe.utils import get_datetime, get_system_timezone, now_datetime

from dms_erp.comms.intent import classify_message
from dms_erp.comms.utils import MESSAGE_TEMPLATES, verify_webhook_secret
from dms_erp.pagination import clamp
from dms_erp.phone_utils import dealer_for_phone

COMMS_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
AUTO_REPLY_INTENTS = {"availability_check", "price_check"}


def _parse_sent_at(sent_at):
	"""whats91 (and potentially other middleware) sends ISO 8601 timestamps with a
	"Z"/UTC offset ("2026-06-05T10:30:00.000Z") -- two separate problems, not one:
	(1) MariaDB's Datetime column rejects the raw string outright as a SQL error,
	not a graceful fallback, so any caller-supplied value always goes through
	Frappe's own flexible datetime parser first; (2) that parser preserves the
	timezone as an *aware* datetime, which MariaDB's Datetime column also rejects
	outright (a different SQL error) since the column stores naive values that,
	everywhere else in this app (now_datetime()), mean local system time, not UTC
	-- inserting a bare UTC value here would both fail the insert and, had it not,
	have silently misordered this dealer's inbound messages against their
	outbound ones by the system's UTC offset. So an aware result is converted to
	system-timezone wall-clock time and then stripped to naive to match. Falls
	back to "now" for a genuinely unparseable value rather than losing the whole
	inbound message over a timestamp that couldn't be made sense of -- same
	fail-soft-on-metadata philosophy as clean_indian_mobile/_is_send_successful
	elsewhere in this app."""
	if not sent_at:
		return now_datetime()
	try:
		parsed = get_datetime(sent_at)
	except Exception:
		return now_datetime()
	if parsed.tzinfo is not None:
		from zoneinfo import ZoneInfo

		parsed = parsed.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
	return parsed


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


def _message_filters(dealer: str) -> dict:
	return {"dealer": dealer}


def list_all_messages(dealer: str) -> list[dict]:
	"""Unpaginated — for internal callers (last_message, unreplied_inbound_count)
	that need the full thread, not a page of it. list_messages (the whitelisted
	endpoint) is the paginated one."""
	filters = _message_filters(dealer)
	names = frappe.get_all("WhatsApp Message", filters=filters, pluck="name", order_by="sent_at asc")
	return [_serialize(frappe.get_doc("WhatsApp Message", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_messages(dealer: str, search: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	# WhatsApp Message's own name is a random hash (autoname: hash) — search matches
	# the message text itself instead, since that's what a user would search for.
	filters = _message_filters(dealer)
	if search:
		filters["text"] = ["like", f"%{search}%"]
	total = frappe.db.count("WhatsApp Message", filters=filters)
	names = frappe.get_all(
		"WhatsApp Message", filters=filters, pluck="name", order_by="sent_at asc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("WhatsApp Message", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def last_message(dealer: str):
	thread = list_all_messages(dealer)
	return thread[-1] if thread else None


@frappe.whitelist(methods=["GET"])
def unreplied_inbound_count(dealer: str) -> int:
	thread = list_all_messages(dealer)
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


def _send_message(
	dealer: str, text: str, related_type: str = "General", related_reference: str | None = None, sent_by: str | None = None
) -> dict:
	"""Unguarded core of send_message -- also called directly by auth.dealer_api's OTP
	delivery, which runs as Guest (no staff session, so _assert_can_manage_comms would
	reject it) and isn't sent "by" any staff user."""
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
			"sent_by": sent_by,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def send_message(dealer: str, text: str, related_type: str = "General", related_reference: str | None = None):
	_assert_can_manage_comms()
	return _send_message(dealer, text, related_type, related_reference, sent_by=frappe.session.user)


@frappe.whitelist(methods=["POST", "PUT"])
def mark_read(message: str):
	_assert_can_manage_comms()

	doc = frappe.get_doc("WhatsApp Message", message)
	doc.status = "Read"
	doc.save(ignore_permissions=True)
	return _serialize(doc)


def _log_inbound_message(
	dealer: str, text: str, related_type: str = "General", related_reference: str | None = None, sent_at=None
) -> dict:
	"""Unguarded core of webhook_inbound_message -- also called directly by
	comms.flow_api, whose Flow-driven callers authenticate as a real Frappe user via
	API key/secret rather than this module's own webhook secret, so they have no
	reason to go through webhook_inbound_message's secret check at all."""
	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"dealer": dealer,
			"direction": "Inbound",
			"text": text,
			"status": "Delivered",  # arrived, not yet marked read by staff — see mark_read
			"related_type": related_type,
			"related_reference": related_reference,
			"sent_at": _parse_sent_at(sent_at),
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook_inbound_message(
	secret: str,
	dealer: str | None = None,
	phone: str | None = None,
	text: str = "",
	related_type: str = "General",
	related_reference: str | None = None,
	sent_at=None,
):
	"""Called by the WhatsApp middleware when a dealer sends a new message.

	Pass either `dealer` (a Customer id, if the caller already resolved it) or
	`phone` (the raw sender number WhatsApp/whats91/whatever flow tool handed it) —
	most callers only ever have the phone, since they have no visibility into this
	app's dealer records, so `phone` is resolved the same normalized way as OTP
	login (phone_utils.dealer_for_phone). Throws when neither resolves to a known
	dealer, rather than silently logging an orphan message no one could ever find
	on a dealer's thread again."""
	verify_webhook_secret(secret)

	if not dealer and phone:
		dealer = dealer_for_phone(phone)
	if not dealer:
		frappe.throw(_("Could not resolve a dealer for this message (phone {0}).").format(phone), frappe.ValidationError)

	message = _log_inbound_message(dealer, text, related_type, related_reference, sent_at)
	try:
		_maybe_auto_reply(dealer, text)
	except Exception:
		# The auto-reply path is a bonus on top of this webhook's real contract
		# (logging the inbound message, already done above) -- a bug or an LLM/DB
		# error here must never surface as a webhook failure to the caller.
		frappe.log_error(title="WhatsApp auto-reply failed")
	return message


def _maybe_auto_reply(dealer: str, text: str):
	"""See this module's own docstring for the full reasoning. Every early return
	below means the same thing: "don't guess, leave it for a human" — that's the
	expected, routine outcome for most messages, not a failure."""
	from dms_erp.catalog.api import resolve_item_mention
	from dms_erp.sales.inquiry_api import _create_inquiry
	from dms_erp.warehouse.utils import total_stock_for_item

	classification = classify_message(text)
	if not classification or classification["intent"] not in AUTO_REPLY_INTENTS:
		return
	mention = classification.get("item_mention")
	if not mention:
		return

	item = resolve_item_mention(dealer, mention)
	if not item:
		return

	try:
		_create_inquiry(dealer=dealer, item=item["id"], qty=1, source="WhatsApp")
	except (frappe.PermissionError, frappe.ValidationError):
		# Not in this dealer's assigned catalog, or no longer sellable -- can't
		# confidently auto-reply "yes it's available" here either.
		return

	on_hand = total_stock_for_item(item["id"])
	if on_hand > 0:
		reply = _(
			"Good news — {0} is back in stock ({1} boxes available). Let us know if you'd like to confirm the order."
		).format(item["name"], int(on_hand))
	else:
		reply = _(
			"{0} is currently out of stock. We'll notify you as soon as it's back — let us know if you'd like us to check alternatives."
		).format(item["name"])
	_send_message(dealer, reply, related_type="Inquiry")


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
