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
direct answer either way; only the internal demand-tracking side effect is skipped.

get_order_status/get_delivery_status/get_outstanding_due/get_recent_orders are the
account-lookup half of the Flow (own Sales Order data, not the catalog) -- no item
resolution or Inquiry-raising involved, just the same dealer_for_phone resolution
plus, for the two order-number lookups, an ownership check
(_find_dealer_order) equivalent to sales.dealer_portal_api.get_my_order's own, so a
dealer can never fish for another dealer's order by guessing a number. The latter
two fire straight off the Flow's main menu with no preceding "wait for input" step,
so there's no dealer-typed text worth logging as an inbound turn for either."""

import json
import re
from collections.abc import Callable

import frappe

from dms_erp.comms.api import _log_inbound_message, _send_message
from dms_erp.phone_utils import dealer_for_phone

_ITEM_LIST_SPLIT_RE = re.compile(r"\s*(?:,|;|&|\n|\band\b|\baur\b|और)\s*", re.IGNORECASE)


def _parse_lead_dict(lead) -> dict:
	if isinstance(lead, str):
		try:
			lead = json.loads(lead)
		except ValueError:
			lead = {}
	return lead or {}


def _lead_fields(lead) -> tuple[str | None, str]:
	lead = _parse_lead_dict(lead)
	phone = lead.get("phone")
	text = (lead.get("message") or "").strip()
	return phone, text


def _lead_variables(lead) -> dict:
	"""whats91's payload_template embeds {{lead.variables}} as a single string slot
	inside the outer JSON body -- whats91's own serialization of everything an
	action.set_variable node has stored so far in this Flow session (order_qty_band,
	order_note, order_item_code, ...). Parses it defensively: only lead.message/phone
	have actually been confirmed against real whats91 traffic so far (see this
	module's own history -- more than one "per the docs" assumption about this
	platform turned out wrong until checked against a real payload), so an
	unparseable or missing value here just means no extra context was available,
	not an error."""
	variables = _parse_lead_dict(lead).get("variables")
	if isinstance(variables, str):
		try:
			variables = json.loads(variables)
		except ValueError:
			variables = {}
	return variables if isinstance(variables, dict) else {}


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


def _find_dealer_order(dealer: str, order_number: str):
	"""Looks up a Sales Order by number and confirms it belongs to `dealer`, the same
	ownership check dealer_portal_api.get_my_order already enforces for its own,
	session-scoped case. Returns None for BOTH "no such order" and "exists but isn't
	yours" -- a caller should give the same generic reply either way rather than
	confirming a valid order number exists for a dealer that isn't asking about it.
	Checks existence with frappe.db.exists first rather than letting frappe.get_doc
	raise, since a mistyped order number is the routine case here, not an error."""
	if not frappe.db.exists("Sales Order", order_number):
		return None
	doc = frappe.get_doc("Sales Order", order_number)
	if doc.customer != dealer:
		return None
	return doc


@frappe.whitelist()
def get_order_status(lead=None, **kwargs):
	"""Flow event `order.status` ("3. Order Status" step): `lead.message` is the
	Sales Order number the dealer typed (e.g. SAL-ORD-2026-00001)."""
	phone, text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}
	if not text:
		return {"message": "Please enter your Sales Order number to check its status."}

	_log_inbound_message(dealer, text, related_type="General")

	doc = _find_dealer_order(dealer, text.strip())
	if not doc:
		reply = f"We couldn't find an order '{text}' on your account. Please check the order number and try again."
		_send_message(dealer, reply, related_type="General")
		return {"message": reply}

	stage = doc.custom_fulfillment_stage or "Confirmed"
	reply = f"Order {doc.name} is currently: {stage}."
	_send_message(dealer, reply, related_type="Order", related_reference=doc.name)
	return {"message": reply}


@frappe.whitelist()
def get_delivery_status(lead=None, **kwargs):
	"""Flow event `delivery.status` ("2. Delivery Status" step): same underlying
	data as get_order_status -- custom_fulfillment_stage is the one status
	vocabulary this app uses for both order and delivery status (order_api.py's own
	docstring notes it's "layered on top of, not derived from, ERPNext's own
	delivery/billing status" -- no separate Delivery Note or dispatch_status field
	exists) -- but framed around dispatch/delivery, and, when the order has actually
	reached that stage, the real timestamp from custom_stage_history rather than
	just the stage name."""
	phone, text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}
	if not text:
		return {"message": "Please enter your Sales Order number to check its delivery status."}

	_log_inbound_message(dealer, text, related_type="General")

	doc = _find_dealer_order(dealer, text.strip())
	if not doc:
		reply = f"We couldn't find an order '{text}' on your account. Please check the order number and try again."
		_send_message(dealer, reply, related_type="General")
		return {"message": reply}

	stage = doc.custom_fulfillment_stage or "Confirmed"
	if stage in ("Dispatched", "Delivered"):
		at = next((row.at for row in reversed(doc.custom_stage_history) if row.stage == stage), None)
		when = f" on {at.strftime('%d %b %Y')}" if at else ""
		reply = f"Order {doc.name} was {stage.lower()}{when}."
	elif stage == "Cancelled":
		reply = f"Order {doc.name} was cancelled."
	else:
		reply = f"Order {doc.name} hasn't been dispatched yet — current stage: {stage}."
	_send_message(dealer, reply, related_type="Order", related_reference=doc.name)
	return {"message": reply}


@frappe.whitelist()
def get_outstanding_due(lead=None, **kwargs):
	"""Flow event `outstanding.check` ("4. Payment Due" step) -- this node fires
	straight off the main menu button with no preceding "wait for input" step (see
	the Flow's own edges), so there's no dealer-typed text to log as an inbound
	turn here, only the reply. Reuses dealer_portal_api._outstanding_for rather
	than duplicating its SQL -- that function's own docstring covers why it's real,
	correct SQL that simply returns 0 today."""
	from dms_erp.sales.dealer_portal_api import _outstanding_for

	phone, _text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}

	outstanding = _outstanding_for(dealer)
	if outstanding > 0:
		reply = f"Your outstanding due is ₹{outstanding:,.2f}."
	else:
		reply = "You have no outstanding dues at the moment."

	_send_message(dealer, reply, related_type="General")
	return {"message": reply}


@frappe.whitelist()
def get_recent_orders(lead=None, **kwargs):
	"""Flow event `orders.recent5` ("6. Recent 5 Orders" step) -- also fires
	straight off the main menu button, no dealer-typed input to log here either."""
	phone, _text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}

	names = frappe.get_all(
		"Sales Order", filters={"customer": dealer}, fields=["name", "custom_fulfillment_stage"], order_by="creation desc", limit_page_length=5
	)
	if not names:
		reply = "You don't have any orders yet."
	else:
		lines = [f"{row.name}: {row.custom_fulfillment_stage or 'Confirmed'}" for row in names]
		reply = f"Your last {len(names)} order(s):\n" + "\n".join(lines)

	_send_message(dealer, reply, related_type="General")
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


# Approximate order qty for a Place Order request -- the Flow only collects a range
# ("10 - 50 units"), not an exact figure; BRD C.2.2's own sample conversation shows a
# real Sales Order being created straight from this WhatsApp exchange with no further
# quantity negotiation step, so a documented midpoint is the pragmatic reading of "how
# many" until/unless the Flow is changed to ask for an exact number instead.
_QTY_BAND_MIDPOINT = {
	"10 - 50 units": 30,
	"50 - 100 units": 75,
	"100 - 200 units": 150,
	"200 - 500 units": 350,
	"500+ units": 500,
}


@frappe.whitelist()
def create_dealer_opportunity(lead=None, **kwargs):
	"""Flow event `opportunity.create` -- used by BOTH the "🔔 Request More Info" step
	(after a stock/price check) and the "🛒 Place Order" step (after quantity-band +
	note selection); the Flow routes both to this same node.

	Per BRD C.2.2's own WhatsApp inquiry flow ("Please share your PO number to
	confirm... Sales Order created; confirmation sent"), placing an order is meant to
	create a real Sales Order once the dealer supplies their own PO number as
	confirmation -- not just another enquiry. The Flow doesn't collect a PO number
	anywhere yet (no node sets one into lead.variables) -- until it's updated with an
	"Ask PO Number" step in the Place-Order branch that stores it as
	lead.variables.po_number, this always takes the Inquiry branch below, which is
	the correct, safe behavior in the meantime (an Inquiry is exactly BRD C.2.2's own
	"item out of stock"/general-enquiry outcome, and nothing here should invent a PO
	number that doesn't exist).

	item is resolved from lead.variables.order_item_code (set earlier in the Flow
	session by whichever check -- stock or price -- the dealer came from), not
	lead.message -- unlike every other endpoint in this module, whose lead.message
	IS the dealer's direct answer to a single question. By the time this node fires,
	lead.message reflects whatever was typed/tapped at the qty/note step instead, so
	order_item_code (a value this Flow's own action.set_variable nodes set
	specifically for this purpose) is the reliable source."""
	from dms_erp.sales.inquiry_api import _create_inquiry
	from dms_erp.sales.order_api import _create_order

	phone, text = _lead_fields(lead)
	variables = _lead_variables(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}

	item_code = variables.get("order_item_code")
	if not item_code:
		return {"message": "We couldn't tell which item this enquiry is for. Please start again from the main menu."}

	if text:
		_log_inbound_message(dealer, text, related_type="General")

	po_number = variables.get("po_number")
	note = variables.get("order_note") or text or None

	if po_number:
		from frappe.utils import add_days, today

		qty = _QTY_BAND_MIDPOINT.get(variables.get("order_qty_band"), 1)
		try:
			order = _create_order(
				dealer=dealer, lines=[{"item": item_code, "qty": qty}], expected_dispatch=add_days(today(), 7), customer_po=po_number
			)
		except (frappe.PermissionError, frappe.ValidationError) as e:
			reply = f"We couldn't place this order: {e}. Please contact your Pacific representative."
			_send_message(dealer, reply, related_type="General")
			return {"message": reply}

		reply = f"Order {order['number']} has been placed against your PO {po_number}. We'll confirm dispatch shortly."
		_send_message(dealer, reply, related_type="Order", related_reference=order["number"])
		return {"message": reply}

	try:
		inquiry = _create_inquiry(dealer=dealer, item=item_code, qty=1, source="WhatsApp", remarks=note)
	except (frappe.PermissionError, frappe.ValidationError):
		reply = "We couldn't raise this enquiry against your account. Please contact your Pacific representative."
		_send_message(dealer, reply, related_type="General")
		return {"message": reply}

	reply = "Thanks — we've noted your enquiry and someone from our team will follow up shortly."
	_send_message(dealer, reply, related_type="Inquiry", related_reference=inquiry["id"])
	return {"message": reply}
