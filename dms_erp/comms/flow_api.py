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
does, not as a second, invisible channel (see comms/api.py's own docstring)."""

import json

import frappe

from dms_erp.comms.api import _log_inbound_message, _send_message
from dms_erp.phone_utils import dealer_for_phone


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


@frappe.whitelist()
def get_item_info(lead=None, **kwargs):
	"""Flow event `item.lookup` (Dealer Portal flow's "1. Item Availability" step):
	`lead.message` is the item code the dealer typed after being prompted for one."""
	from dms_erp.catalog.api import resolve_item_mention
	from dms_erp.warehouse.utils import total_stock_for_item

	phone, text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}
	if not text:
		return {"message": "Please enter an item code to check availability."}

	_log_inbound_message(dealer, text, related_type="General")

	item = resolve_item_mention(dealer, text)
	if not item:
		reply = f"We couldn't find an item matching '{text}'. Please check the item code and try again."
	else:
		on_hand = total_stock_for_item(item["id"])
		if on_hand > 0:
			reply = f"{item['name']} ({item['code']}) is in stock — {int(on_hand)} boxes available."
		else:
			reply = f"{item['name']} ({item['code']}) is currently out of stock."

	_send_message(dealer, reply, related_type="General")
	return {"message": reply}
