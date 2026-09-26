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
so each function here returns a plain dict (`{"message": "<reply text>"}`, plus an
`item_code` key on get_item_info -- see _resolve_and_track_items' own docstring for
why) rather than unwrapping it the way comms.whats91 does for its own, different
caller.

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

get_item_info raises a real Inquiry (source "WhatsApp") for every resolved item,
same as comms.api._maybe_auto_reply already does for the free-text path (which
treats availability and price checks identically) -- without it, a Flow-driven
"is this in stock" check is invisible to purchase.reorder_api's missedDemandQty
(Inquiry qty already Out of Stock/Pre-order Required at creation, per
sales.inquiry_api's own status-from-stock derivation), which is the actual reorder
signal a dealer typing "Royal Glass" into the Flow is supposed to feed. Unlike
_maybe_auto_reply, a failure to create one (not in this dealer's catalog, not
sellable) doesn't cancel the reply -- the dealer asked a direct question and gets a
direct answer either way; only the internal demand-tracking side effect is skipped.

A dealer prompted once for "the item code" often answers with several at once
("RUSTIC-GREY, Royal Glossy", or "RUSTIC-GREY और Royal Glossy" in Hindi) --
_split_item_mentions splits on the obvious list delimiters (comma, "and"/"aur"/"और",
"&", newline) and each segment is resolved independently (one line, one Inquiry
each) via the shared _resolve_and_track_items core, rather than treating the whole
message as a single lookup that only ever finds the first item and silently drops
the rest. Deterministic splitting, not an LLM call -- this is a list-parsing
problem, not an intent-understanding one, and the free-text LLM path's own
item_mention is deliberately single-item already (see comms/intent.py).

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

# BRD C.2.2's own sample conversation folds a quantity into the same message as the
# item ("White tiles 40 boxes available?") -- requires an explicit unit word, never
# bare trailing digits, since this app's own item codes routinely end in digits
# (GVT-6013, AAS-001) and would otherwise be misread as a quantity.
_QTY_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:box(?:es)?|pcs?|pieces?|units?)\s*$", re.IGNORECASE)


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


def _extract_qty(segment: str) -> tuple[str, float | None]:
	"""Strips a trailing "<n> boxes/pcs/units" quantity off a single item-lookup
	segment, e.g. "GVT-6013 40 boxes" -> ("GVT-6013", 40.0) -- see _QTY_UNIT_RE's own
	comment for why bare trailing digits are deliberately left alone. Returns the
	segment unchanged with qty=None when no unit word is present, so every message
	that predates this (a bare code or name) resolves exactly as it always has."""
	m = _QTY_UNIT_RE.search(segment)
	if not m:
		return segment, None
	return segment[: m.start()].strip(), float(m.group(1))


def _resolve_and_track_items(
	dealer: str, text: str, describe_item: Callable[[dict, float | None], str]
) -> tuple[str, str, str | None, str | None]:
	"""Core of get_item_info -- factored out on its own so the split/resolve/Inquiry-
	raising logic (the part that's genuinely reusable) stays separate from the
	reply-building logic (the part that isn't, once availability and price were
	merged into one endpoint). `describe_item(item, qty)` builds the one reply line
	for a single resolved item -- qty is whatever _extract_qty pulled off that same
	segment, or None when the dealer didn't include one. Returns (reply_text,
	related_type, related_reference, item_code).

	item_code is the first successfully resolved item's code, or None -- the Flow's
	own pre-existing n_set_stock_order_ref node reads this back as
	erpnext_response.message.item_code to carry the item forward into "🛒 Place
	Order" (see create_dealer_opportunity, which consumes it as
	lead.variables.order_item_code). Without it in the response, that template
	silently resolves to nothing and whats91 falls back to whatever text was
	actually on hand -- observed in production as the literal button label ("🛒
	Place Order") being treated as the item code all the way through to order
	creation. A multi-item request only ever carries one order forward regardless,
	so "the first resolved item" is the only sane choice here, not a compromise.

	BRD C.2.1: "on a mismatch or multiple candidates, the user/dealer is prompted to
	confirm the exact code rather than the system guessing." resolve_item_by_name's
	"ambiguous" status (a weak best match, or a close runner-up -- see its own
	docstring) is never silently resolved into an answer here: no Inquiry is raised
	for that segment (there's no confirmed item to raise one against), item_code
	isn't set from it, and the reply lists the actual candidates by name and code so
	the dealer can just retype the exact one. Deliberately not a real multi-turn
	confirmation loop (a dealer tapping "🛒 Place Order" straight after with no
	item_code set falls through to create_dealer_opportunity's own existing "we
	couldn't tell which item" reply, itself already correct for exactly this) --
	whats91's action.api_call nodes only expose two output branches (Success/Error),
	confirmed against this Flow's own JSON, so a genuine "reply 1 or 2" branch isn't
	buildable without unconfirmed platform behavior. This closes BRD's actual
	correctness requirement (never guess) without pretending the interactive
	round-trip is built too."""
	from dms_erp.catalog.api import resolve_item_by_name, resolve_item_mention
	from dms_erp.sales.inquiry_api import _create_inquiry

	lines = []
	created_inquiry_ids = []
	item_code = None
	for raw_segment in _split_item_mentions(text):
		segment, qty = _extract_qty(raw_segment)
		# A dealer prompted for "the item code" often types the item's name instead,
		# sometimes with a typo -- the exact code match is tried first since it's the
		# intended, unambiguous path; the fuzzy name match is only a fallback for when
		# that fails (see resolve_item_by_name's own docstring for why this fallback
		# doesn't also apply to the free-text LLM path).
		item = resolve_item_mention(dealer, segment)
		if not item:
			outcome = resolve_item_by_name(dealer, segment)
			if outcome["status"] == "matched":
				item = outcome["item"]
			elif outcome["status"] == "ambiguous":
				names = ", ".join(f"{c['name']} ({c['code']})" for c in outcome["candidates"])
				lines.append(
					f"We found more than one possible match for '{segment}': {names}. "
					f"Please reply with the exact item code you meant."
				)
				continue
			else:
				lines.append(f"We couldn't find an item matching '{segment}'. Please check the item code and try again.")
				continue

		if item_code is None:
			item_code = item["id"]

		try:
			inquiry = _create_inquiry(dealer=dealer, item=item["id"], qty=qty or 1, source="WhatsApp")
			created_inquiry_ids.append(inquiry["id"])
		except (frappe.PermissionError, frappe.ValidationError):
			# Not in this dealer's assigned catalog, or no longer sellable -- the
			# dealer still gets a direct answer below; only the internal
			# demand-tracking side effect is skipped (see this module's docstring).
			pass

		lines.append(describe_item(item, qty))

	reply = "\n".join(lines)
	# A single reference field can't point at more than one Inquiry -- only tag the
	# message against a specific one when there's exactly one candidate.
	if len(created_inquiry_ids) == 1:
		return reply, "Inquiry", created_inquiry_ids[0], item_code
	return reply, "General", None, item_code


def _alt_item_suggestion(dealer: str, item: dict) -> str | None:
	"""BRD C.2.2's out-of-stock reply includes "a suggested alternative" --
	catalog.api._get_alt_item (already surfaced as item['altItemId'] on every
	resolved item -- catalog.api's own _serialize, whether it came directly from
	resolve_item_mention or from resolve_item_by_name's "matched" status) is the
	existing two-way Item Alternative link. Only worth
	mentioning when it's both actually in this dealer's own catalog (never
	recommend an item they can't even see/order, same visibility boundary as the
	item lookup itself) and itself has real stock -- suggesting another dead end
	isn't an alternative at all. Returns None (not a placeholder) whenever either
	check fails, same "don't guess, say nothing" convention as elsewhere in this
	module."""
	alt_id = item.get("altItemId")
	if not alt_id:
		return None

	from dms_erp.catalog.dealer_catalog_api import catalog_for
	from dms_erp.warehouse.utils import total_stock_for_item

	if alt_id not in catalog_for(dealer):
		return None
	if total_stock_for_item(alt_id) <= 0:
		return None

	alt_name = frappe.db.get_value("Item", alt_id, "item_name")
	return f"{alt_name} ({alt_id})"


@frappe.whitelist()
def get_item_info(lead=None, **kwargs):
	"""Flow event `item.lookup` (Dealer Portal flow's "1. Item Availability & Price"
	step): `lead.message` is the item code(s) the dealer typed after being prompted
	for one -- see this module's own docstring for how more than one is handled.

	A single combined reply (availability + size/finish + price) per BRD C.2.2's own
	illustrated exchange -- item availability and price used to be two separate menu
	steps/endpoints (get_item_price), which was just a shape mismatch against the
	one-reply BRD conversation, not a missing capability; merged here rather than
	keeping a second endpoint alive that would otherwise go unused. custom_price_visible
	still gates the price line the same way it already gates price display in the
	dealer-portal catalog (sales/dealer_api.py) -- a dealer whose account has prices
	hidden sees stock/size/finish only, no price line at all, never a placeholder.

	Out of stock also surfaces the item's own lead_time_days and, when a real one
	is available, a suggested alternative (see _alt_item_suggestion) -- BRD C.2.2's
	out-of-stock reply is "expected lead time and a suggested alternative", not just
	a bare "out of stock".

	When the dealer's message also carried a quantity (see _extract_qty -- BRD
	C.2.2's own sample conversation folds one in: "White tiles 40 boxes available?"),
	an in-stock reply names the specific batch (or 2-3 batch combination,
	suggest_batch_combination) that covers it, rather than just the flat total
	across every batch -- a dealer asking for 40 boxes doesn't actually learn
	anything useful from "45 available" if that's scattered across five different
	lots. No quantity given -- the dealer just typed a code/name -- falls back to
	that same flat total exactly as before this was added."""
	from dms_erp.pricing.api import get_price_for_dealer
	from dms_erp.warehouse.utils import suggest_batch_combination, total_stock_for_item

	phone, text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}
	if not text:
		return {"message": "Please enter an item code to check availability and price."}

	_log_inbound_message(dealer, text, related_type="General")

	price_visible = bool(frappe.db.get_value("Customer", dealer, "custom_price_visible"))

	def describe(item, qty):
		label = f"{item['name']} ({item['code']})"
		if item.get("size") or item.get("finish"):
			label += f" — {item.get('size') or '—'} / {item.get('finish') or '—'}"

		on_hand = total_stock_for_item(item["id"])
		if on_hand <= 0:
			line = f"{label} is currently out of stock."
			lead_time = item.get("leadTimeDays")
			if lead_time:
				line += f" Expected lead time: {int(lead_time)} day{'s' if lead_time != 1 else ''}."
			alt = _alt_item_suggestion(dealer, item)
			if alt:
				line += f" You may also consider {alt}."
			return line

		combo = suggest_batch_combination(item["id"], qty) if qty else None
		if combo and combo["batches"]:
			parts = " + ".join(f"Batch {b['batchNumber']} ({int(b['boxes'])})" for b in combo["batches"])
			if combo["sufficient"]:
				line = f"{label} is in stock. For your {int(qty)} boxes, we suggest {parts} — {int(combo['totalBoxes'])} boxes total."
			else:
				line = f"{label} only has {int(combo['totalBoxes'])} boxes across its available batches — short of your {int(qty)}."
		else:
			# No quantity given, or on_hand is real Bin stock with no matching Batch
			# lots to break it down by (a data inconsistency, not the common case) --
			# either way the flat total is the only thing there's data for.
			line = f"{label} is in stock — {int(on_hand)} boxes available."

		if price_visible:
			rate = get_price_for_dealer(item["id"], dealer)
			if rate is not None:
				line += f" Price: ₹{rate:,.2f} per box."
		return line

	reply, related_type, related_reference, item_code = _resolve_and_track_items(dealer, text, describe)
	_send_message(dealer, reply, related_type=related_type, related_reference=related_reference)
	return {"message": reply, "item_code": item_code}


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
	"""Flow event `orders.recent5` ("5. Recent 5 Orders" step) -- also fires
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
	session by the availability/price check the dealer came from -- get_item_info),
	not lead.message -- unlike every other endpoint in this module, whose
	lead.message IS the dealer's direct answer to a single question. By the time
	this node fires, lead.message reflects whatever was typed/tapped at the qty/note
	step instead, so order_item_code (a value this Flow's own action.set_variable
	nodes set specifically for this purpose) is the reliable source."""
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
