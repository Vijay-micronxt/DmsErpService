"""Adapter for whats91's Flow engine's `action.api_call` nodes -- a different whats91
mechanism from comms.whats91.receive_webhook (raw inbound-message events). A Flow (see
e.g. the "Dealer Portal" flow: keyword trigger -> menu -> "enter your item code" ->
wait -> API call) runs its whole conversation -- menus, waiting for input, branching --
entirely on whats91's own servers. It never sends us a message.inbound.text webhook at
all; it only calls out to ERPNext when a step needs real data, and only for that one
exchange (the dealer's last typed reply, not the whole conversation history).

Each Flow node POSTs:
    {"lead": {"event": ..., "phone": "{{contact.phone}}", "message": "{{lead.message}}",
              "variables": "{{lead.variables}}"}}
and reads the reply from Frappe's own `{"message": {...}}` auto-wrap of a whitelisted
method's return value -- the Flow's own templates expect `erpnext_response.message.message`,
so each function here returns a plain `{"message": "<reply text>"}` rather than
unwrapping it the way comms.whats91 does for its own, different caller.

Each endpoint is registered under a short, dotted-free name (get_item_info, not
dms_erp.comms.flow_api.get_item_info) via hooks.py's override_whitelisted_methods,
matching the Flow's *existing* endpoint_url config exactly rather than requiring every
node to be re-pointed at a fully-dotted path.

Authenticated the ordinary Frappe way -- `Authorization: token <api_key>:<api_secret>`,
configured directly in the Flow's own headers_template -- unlike comms.whats91's
allow_guest=True plus a custom header-token scheme, since these are already real,
authenticated Frappe API requests rather than anonymous webhook deliveries. No
allow_guest here as a result: an unauthenticated caller is rejected by Frappe itself
before this module ever runs.

Every call also logs the dealer's turn and our reply as a genuine WhatsApp Message
(comms.api._log_inbound_message/_send_message) -- the whole point of building these is
so a Flow-driven conversation shows up in Communications exactly like a free-text one
does, not as a second, invisible channel (see comms/api.py's own docstring).

get_item_info also raises a real Inquiry (source "WhatsApp") for every resolved item,
same as comms.api._maybe_auto_reply already does for the free-text path -- without it,
a Flow-driven "is this in stock" check is invisible to purchase.reorder_api's
missedDemandQty (Inquiry qty already Out of Stock/Pre-order Required at creation, per
sales.inquiry_api's own status-from-stock derivation), which is the actual reorder
signal a dealer typing "Royal Glass" into the Flow is supposed to feed. Unlike
_maybe_auto_reply, a failure to create one (not in this dealer's catalog, not
sellable) doesn't cancel the reply -- the dealer asked a direct question and gets a
direct answer either way; only the internal demand-tracking side effect is skipped.

A dealer prompted once for "the item code" often answers with several at once
("RUSTIC-GREY, Royal Glossy", or "RUSTIC-GREY और Royal Glossy" in Hindi) --
_split_item_mentions splits on the obvious list delimiters (comma, "and"/"aur"/"और",
"&", newline) and each segment is resolved independently (one line, one Inquiry
each), rather than treating the whole message as a single lookup that only ever
finds the first item and silently drops the rest. Deterministic splitting, not an
LLM call -- this is a list-parsing problem, not an intent-understanding one, and the
free-text LLM path's own item_mention is deliberately single-item already (see
comms/intent.py).

get_item_info and get_item_price share that same split-resolve-track shape
(_resolve_and_track_items) -- only the per-item reply line differs (stock count vs.
price), so it's factored out once rather than duplicated per endpoint. Both raise a
real Inquiry (source "WhatsApp") for every resolved item, same as comms.api's
_maybe_auto_reply already does for the free-text path (which treats availability and
price checks identically): without it, a Flow-driven check is invisible to
purchase.reorder_api's missedDemandQty (Inquiry qty already Out of Stock/Pre-order
Required at creation, per sales.inquiry_api's own status-from-stock derivation),
which is the actual reorder signal these checks are supposed to feed. Unlike
_maybe_auto_reply, a failure to create one (not in this dealer's catalog, not
sellable) doesn't cancel the reply -- the dealer asked a direct question and gets a
direct answer either way; only the internal demand-tracking side effect is skipped."""

import json
import re
from collections.abc import Callable

import frappe

from dms_erp.comms.api import _log_inbound_message, _send_message
from dms_erp.phone_utils import dealer_for_phone

_ITEM_LIST_SPLIT_RE = re.compile(r"\s*(?:,|;|&|\n|\band\b|\baur\b|और)\s*", re.IGNORECASE)


def _lead_fields(lead) -> tuple[str | None, str]:
	if isinstance(lead, str):
		try:
			lead = json.loads(lead)
		except ValueError:
			lead = {}
	lead = lead or {}
	phone = lead.get("phone")
	text = (lead.get("message") or "").strip()
	return phone, text


def _split_item_mentions(text: str) -> list[str]:
	segments = [s.strip() for s in _ITEM_LIST_SPLIT_RE.split(text) if s.strip()]
	return segments or [text.strip()]


def _resolve_and_track_items(dealer: str, text: str, describe_item: Callable[[dict], str]) -> tuple[str, str, str | None]:
	"""Shared core of get_item_info/get_item_price -- see this module's own docstring
	for why. `describe_item(item)` builds the one reply line for a single resolved
	item; everything else (splitting, resolution, Inquiry-raising, and picking
	related_type/related_reference for the eventual WhatsApp Message) is identical
	between callers. Returns (reply_text, related_type, related_reference)."""
	from dms_erp.catalog.api import resolve_item_by_name, resolve_item_mention
	from dms_erp.sales.inquiry_api import _create_inquiry

	lines = []
	created_inquiry_ids = []
	for segment in _split_item_mentions(text):
		# A dealer prompted for "the item code" often types the item's name instead,
		# sometimes with a typo -- the exact code match is tried first since it's the
		# intended, unambiguous path; the fuzzy name match is only a fallback for when
		# that fails (see resolve_item_by_name's own docstring for why this fallback
		# doesn't also apply to the free-text LLM path).
		item = resolve_item_mention(dealer, segment) or resolve_item_by_name(dealer, segment)
		if not item:
			lines.append(f"We couldn't find an item matching '{segment}'. Please check the item code and try again.")
			continue

		try:
			inquiry = _create_inquiry(dealer=dealer, item=item["id"], qty=1, source="WhatsApp")
			created_inquiry_ids.append(inquiry["id"])
		except (frappe.PermissionError, frappe.ValidationError):
			# Not in this dealer's assigned catalog, or no longer sellable -- the
			# dealer still gets a direct answer below; only the internal
			# demand-tracking side effect is skipped (see this module's docstring).
			pass

		lines.append(describe_item(item))

	reply = "\n".join(lines)
	# A single reference field can't point at more than one Inquiry -- only tag the
	# message against a specific one when there's exactly one candidate.
	if len(created_inquiry_ids) == 1:
		return reply, "Inquiry", created_inquiry_ids[0]
	return reply, "General", None


@frappe.whitelist()
def get_item_info(lead=None, **kwargs):
	"""Flow event `item.lookup` (Dealer Portal flow's "1. Item Availability" step):
	`lead.message` is the item code(s) the dealer typed after being prompted for one --
	see this module's own docstring for how more than one is handled."""
	from dms_erp.warehouse.utils import total_stock_for_item

	phone, text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}
	if not text:
		return {"message": "Please enter an item code to check availability."}

	_log_inbound_message(dealer, text, related_type="General")

	def describe(item):
		on_hand = total_stock_for_item(item["id"])
		if on_hand > 0:
			return f"{item['name']} ({item['code']}) is in stock — {int(on_hand)} boxes available."
		return f"{item['name']} ({item['code']}) is currently out of stock."

	reply, related_type, related_reference = _resolve_and_track_items(dealer, text, describe)
	_send_message(dealer, reply, related_type=related_type, related_reference=related_reference)
	return {"message": reply}


@frappe.whitelist()
def get_item_price(lead=None, **kwargs):
	"""Flow event `price.lookup` (Dealer Portal flow's "5. Item Price" step):
	`lead.message` is the item code(s) the dealer typed after being prompted for one.

	custom_price_visible gates this the same way it already gates price display in
	the dealer-portal catalog (sales/dealer_api.py) -- a dealer whose account has
	prices hidden must not see one over WhatsApp either, even though they can still
	ask "is this in stock" via get_item_info."""
	from dms_erp.pricing.api import get_price_for_dealer

	phone, text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}
	if not text:
		return {"message": "Please enter an item code to check its price."}

	_log_inbound_message(dealer, text, related_type="General")

	price_visible = bool(frappe.db.get_value("Customer", dealer, "custom_price_visible"))

	def describe(item):
		if not price_visible:
			return f"{item['name']} ({item['code']}): pricing isn't available on your account -- please contact your Pacific representative."
		rate = get_price_for_dealer(item["id"], dealer)
		if rate is None:
			return f"{item['name']} ({item['code']}): no price is published for your account yet -- please contact your Pacific representative."
		return f"{item['name']} ({item['code']}): ₹{rate:,.2f} per box."

	reply, related_type, related_reference = _resolve_and_track_items(dealer, text, describe)
	_send_message(dealer, reply, related_type=related_type, related_reference=related_reference)
	return {"message": reply}
