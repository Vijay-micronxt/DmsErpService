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
from dms_erp.sales.utils import dealer_contact_line

_ITEM_LIST_SPLIT_RE = re.compile(r"\s*(?:,|;|&|\n|\band\b|\baur\b|और)\s*", re.IGNORECASE)

# BRD C.2.2's own sample conversation folds a quantity into the same message as the
# item ("White tiles 40 boxes available?") -- requires an explicit unit word, never
# bare trailing digits, since this app's own item codes routinely end in digits
# (GVT-6013, AAS-001) and would otherwise be misread as a quantity.
#
# WhatsApp Inquiry gap #6: dealers also bracket the quantity -- "BCH(220 box)",
# "BCH [220 boxes]" -- and end with a question mark/full stop. Both are accepted:
# an optional opening "("/"[" before the number, an optional matching close and
# trailing ".?!" after the unit word. The unit word itself stays mandatory, for
# the same item-codes-end-in-digits reason as above.
_QTY_UNIT_RE = re.compile(
	r"[(\[]?\s*(\d+(?:\.\d+)?)\s*(?:box(?:es)?|pcs?|pieces?|units?)\s*[)\]]?\s*[.?!]*\s*$", re.IGNORECASE
)


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

	A single combined reply (availability + size/finish) per BRD C.2.2's own
	illustrated exchange. No price is ever included: Pacific's price to a dealer
	depends on delivery and other details that only exist once a quotation is
	prepared, so a price quoted over WhatsApp first just confuses dealers (client
	request, Oct 2026: "don't show price before quotation", WhatsApp included) --
	the dealer-portal catalog drops it for the same reason. BRD C.2.2's illustrated
	reply did carry a price; this is a deliberate departure from it, and
	Customer.custom_price_visible is no longer read here. The price reaches the
	dealer in the quotation.

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
	from dms_erp.warehouse.utils import all_batches, suggest_batch_combination, total_stock_for_item

	phone, text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}
	if not text:
		return {"message": "Please enter an item code to check availability."}

	_log_inbound_message(dealer, text, related_type="General")

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
			# No quantity given (WhatsApp gap #2: "if multiple batches are
			# available, all the batches should list down with the individual
			# quantities") -- every batch, and (client request, Oct 2026: "which
			# batch it belongs to") a single batch is named too rather than collapsed
			# into a bare total. Only on_hand being real Bin stock with no matching
			# Batch lots at all (a data inconsistency, not the common case) falls
			# back to the flat total -- there's no batch to name.
			batches = all_batches(item["id"])
			if len(batches) > 1:
				parts = " + ".join(f"Batch {b['batchNumber']} ({int(b['boxes'])})" for b in batches)
				line = f"{label} is in stock — {parts} ({int(on_hand)} boxes total)."
			elif batches:
				line = f"{label} is in stock — Batch {batches[0]['batchNumber']} ({int(batches[0]['boxes'])} boxes)."
			else:
				line = f"{label} is in stock — {int(on_hand)} boxes available."

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
	than duplicating its ledger query."""
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
# ("10 - 50 units"), not an exact figure -- the Flow's quantity step offers these as
# quick-tap buttons, but also accepts the dealer typing an exact number instead
# (ISS-2026-00645/sheet item #8: "if i want to give custom quantity instead of
# choosing range? its not allowing it"). _resolve_order_qty tries an exact number
# first and only falls back to a band's midpoint when the dealer actually tapped a
# band button -- the approximation is for the fallback case, not the default.
_QTY_BAND_MIDPOINT = {
	"10 - 50 units": 30,
	"50 - 100 units": 75,
	"100 - 200 units": 150,
	"200 - 500 units": 350,
	"500+ units": 500,
}

# A typed reply like "40", "40 boxes", "40 units", or "40.0" -- same shape as
# _QTY_UNIT_RE's trailing-quantity match, but qty can be the whole reply here
# (nothing else to parse out of it), so the unit word is optional.
_QTY_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:box(?:es)?|pcs?|pieces?|units?)?\s*$", re.IGNORECASE)


def _resolve_order_qty(order_qty_band: str | None) -> float:
	"""The Place-Order Flow step offers 5 quick-tap qty-range buttons, but also
	accepts the dealer typing an exact number into the same step instead -- both
	land in lead.variables.order_qty_band, since the Flow stores whichever one the
	dealer actually did into that one variable. A number is used exactly; a
	recognized band label falls back to its documented midpoint; anything else
	(or nothing at all) defaults to 1, same as before this existed."""
	text = (order_qty_band or "").strip()
	m = _QTY_NUMBER_RE.match(text)
	if m:
		qty = float(m.group(1))
		if qty > 0:
			return qty
	return _QTY_BAND_MIDPOINT.get(text, 1)


@frappe.whitelist()
def create_dealer_opportunity(lead=None, **kwargs):
	"""Flow event `opportunity.create` -- used by BOTH the "🔔 Request More Info" step
	(after a stock/price check) and the "🛒 Place Order" step (after quantity-band +
	note selection); the Flow routes both to this same node.

	Per BRD C.2.2's own WhatsApp inquiry flow ("Please share your PO number to
	confirm... Sales Order created; confirmation sent"), placing an order is meant to
	create a real Sales Order once the dealer supplies their own PO number as
	confirmation -- not just another enquiry. The Place-Order branch's "Ask PO Number"
	step stores it as lead.variables.po_number (docs/whatsapp-flows/
	dealer-portal-pacific.json); "Request More Info" never sets one, so that branch
	always takes the Inquiry path below -- nothing here invents a PO number that
	doesn't exist.

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

	quotation_id = variables.get("quotation_id")
	if po_number and quotation_id:
		# The Flow showed the dealer this quotation (create_whatsapp_quotation) and they
		# confirmed it -- the order is built from it, exactly as quoted. A Flow that
		# predates that step never sets quotation_id and still falls through to the
		# direct-order path below, so an un-updated live bot keeps working.
		return _accept_whatsapp_quotation(dealer, quotation_id, po_number)

	if po_number:
		from frappe.utils import add_days, today

		from dms_erp.catalog.dealer_catalog_api import is_visible
		from dms_erp.sales.credit_limit import gate_credit_limit
		from dms_erp.sales.order_api import _estimate_order_value
		from dms_erp.sales.utils import safe_customer_message

		# ISS-2026-00644: an item outside this dealer's assigned Dealer Catalog can be
		# looked up and inquired on over WhatsApp (see catalog.api.resolve_item_by_name),
		# but never ordered through this Place-Order step -- same assigned/orderable
		# boundary dealer_portal_api.convert_to_order enforces for the portal. Falls
		# through to the ordinary Inquiry branch below rather than a hard dead end, so
		# the dealer still gets a useful outcome (staff follow-up) instead of silence.
		if not is_visible(dealer, item_code):
			reply = (
				"This item isn't yet set up for ordering on your account. We've noted your "
				"enquiry and our team will follow up to get it added."
			)
			try:
				inquiry = _create_inquiry(dealer=dealer, item=item_code, qty=1, source="WhatsApp", remarks=note)
				_send_message(dealer, reply, related_type="Inquiry", related_reference=inquiry["id"])
			except (frappe.PermissionError, frappe.ValidationError):
				_send_message(dealer, reply, related_type="General")
			return {"message": reply}

		qty = _resolve_order_qty(variables.get("order_qty_band"))
		lines = [{"item": item_code, "qty": qty}]
		create_kwargs = dict(dealer=dealer, lines=lines, expected_dispatch=add_days(today(), 7), customer_po=po_number)

		try:
			# BRD C.11 trigger #1: a WhatsApp-placed order is never itself an
			# "authorized" credit-limit override (the API user this Flow
			# authenticates as holds no DMS Management/System Manager role) --
			# an over-limit order always queues for Management's decision here,
			# rather than hard-blocking the dealer the way ERPNext's own native
			# credit-limit check would (and, before this, actually did -- see
			# safe_customer_message's own docstring for what that looked like).
			result = gate_credit_limit(
				dealer=dealer,
				additional_value=_estimate_order_value(dealer, lines),
				reference_doctype="Sales Order",
				applier_action="create_order",
				create_fn=_create_order,
				create_kwargs=create_kwargs,
			)
		except (frappe.PermissionError, frappe.ValidationError) as e:
			# Safety net: gate_credit_limit's own estimate can't perfectly predict
			# every real ERPNext validation (taxes, a concurrent order, etc.), so
			# _create_order can still raise here -- never forward that raw text
			# to a dealer.
			reply = safe_customer_message(e, dealer)
			_send_message(dealer, reply, related_type="General")
			return {"message": reply}

		if isinstance(result, dict) and result.get("approvalRequired"):
			reply = (
				f"Your order against PO {po_number} is over your approved credit limit, so it needs "
				f"a quick review by our team before we can confirm it. We'll notify you once it's approved."
			)
			_send_message(dealer, reply, related_type="General")
			return {"message": reply}

		order = result
		reply = f"Order {order['number']} has been placed against your PO {po_number}. We'll confirm dispatch shortly."
		_send_message(dealer, reply, related_type="Order", related_reference=order["number"])
		return {"message": reply}

	try:
		inquiry = _create_inquiry(dealer=dealer, item=item_code, qty=1, source="WhatsApp", remarks=note)
	except (frappe.PermissionError, frappe.ValidationError):
		reply = f"We couldn't raise this enquiry against your account. {dealer_contact_line(dealer)}"
		_send_message(dealer, reply, related_type="General")
		return {"message": reply}

	reply = "Thanks — we've noted your enquiry and someone from our team will follow up shortly."
	_send_message(dealer, reply, related_type="Inquiry", related_reference=inquiry["id"])
	return {"message": reply}


WHATSAPP_QUOTATION_VALIDITY_DAYS = 7


def _quotation_batch_line(doc) -> str | None:
	"""Which batch(es) the quoted quantity would be supplied from -- the same greedy
	largest-first suggestion the stock lookup gives (suggest_batch_combination).
	Indicative only: batches are actually allocated when the order is picked, so the
	wording is "suggested", and a quantity stock can't cover says so rather than
	implying it can. One line per item; nothing at all when the item has no batch
	lots on record."""
	from dms_erp.warehouse.utils import suggest_batch_combination

	parts = []
	for row in doc.items:
		combo = suggest_batch_combination(row.item_code, row.qty)
		if not combo["batches"]:
			continue
		batches = " + ".join(f"Batch {b['batchNumber']} ({int(b['boxes'])})" for b in combo["batches"])
		if combo["sufficient"]:
			parts.append(f"Suggested {batches}")
		else:
			parts.append(f"In stock: {batches} — {int(combo['totalBoxes'])} of {int(row.qty)} boxes")
	return "; ".join(parts) if parts else None


def _quotation_message(doc) -> str:
	"""The quotation as a chat message. Rates are the dealer's own tier rate as
	published (no markup); GST and freight are never part of an automatic quotation,
	so the message says they're extra rather than leaving the total to be read as
	final."""
	from frappe.utils import formatdate

	lines = []
	for row in doc.items:
		qty = int(row.qty) if float(row.qty).is_integer() else row.qty
		lines.append(f"• {row.item_name or row.item_code} ({row.item_code}) — {qty} boxes × ₹{row.rate:,.0f} = ₹{row.amount:,.0f}")
	text = f"📄 Quotation {doc.name}\n" + "\n".join(lines)
	batch_line = _quotation_batch_line(doc)
	if batch_line:
		text += "\n" + batch_line
	text += f"\nTotal: ₹{doc.grand_total:,.0f} (GST and freight extra)"
	if doc.valid_till:
		text += f"\nValid till {formatdate(doc.valid_till, 'dd MMM yyyy')}."
	return text


@frappe.whitelist()
def create_whatsapp_quotation(lead=None, **kwargs):
	"""Flow event `quotation.create` -- the new step between "Place Order"'s quantity/
	note choices and the PO number: instead of asking for a PO and creating a Sales
	Order straight away, the bot now shows the dealer a quotation in the chat and asks
	them to confirm it (client request, Oct 2026: a quotation before the order).

	The quotation is a real one -- a Quotation document built by the same
	quotation_api._create_quotation the staff Quotation Builder uses, so it also shows
	on the dealer portal's Quotes tab and in the staff app, and an Inquiry (with the
	quantity actually asked for) is logged against it, so staff see the demand even if
	the dealer never confirms. It is automatic -- no staff review -- priced at the
	dealer's own tier rate with no markup, GST or freight (the message says so); a
	quotation staff prepare by hand can carry those.

	Returns `quotation_id` ("" when none could be made) alongside the message: the
	Flow keys its next step on it (confirm buttons when set, just the message when
	not), and stores it as lead.variables.quotation_id for create_dealer_opportunity.
	An item outside this dealer's assigned catalog, or one with no approved price
	yet, gets an enquiry instead of a quotation -- the same outcomes the direct-order
	path already had for the first of those."""
	phone, text = _lead_fields(lead)
	variables = _lead_variables(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {
			"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support.",
			"quotation_id": "",
		}

	item_code = variables.get("order_item_code")
	if not item_code:
		return {
			"message": "We couldn't tell which item this is for. Please start again from the main menu.",
			"quotation_id": "",
		}

	if text:
		_log_inbound_message(dealer, text, related_type="General")

	from dms_erp.catalog.dealer_catalog_api import is_visible
	from dms_erp.sales.inquiry_api import _create_inquiry
	from dms_erp.sales.order_channel import auto_classify_channel
	from dms_erp.sales.quotation_api import _create_quotation

	qty = _resolve_order_qty(variables.get("order_qty_band"))
	note = variables.get("order_note") or text or None

	def inquiry_only(reply):
		try:
			inquiry = _create_inquiry(dealer=dealer, item=item_code, qty=qty, source="WhatsApp", remarks=note)
			_send_message(dealer, reply, related_type="Inquiry", related_reference=inquiry["id"])
		except (frappe.PermissionError, frappe.ValidationError):
			_send_message(dealer, reply, related_type="General")
		return {"message": reply, "quotation_id": ""}

	if not is_visible(dealer, item_code):
		return inquiry_only(
			"This item isn't yet set up for ordering on your account. We've noted your "
			"enquiry and our team will follow up to get it added."
		)

	lines = [{"item": item_code, "qty": qty}]
	try:
		inquiry = _create_inquiry(dealer=dealer, item=item_code, qty=qty, source="WhatsApp", remarks=note)
		quotation = _create_quotation(
			dealer,
			lines,
			markup_pct=0,
			validity_days=WHATSAPP_QUOTATION_VALIDITY_DAYS,
			inquiries=[inquiry["id"]],
			channel=auto_classify_channel(dealer, lines),
		)
	except (frappe.PermissionError, frappe.ValidationError):
		reply = (
			"We can't prepare a quotation for this item right now, but we've noted your "
			f"enquiry and our team will follow up. {dealer_contact_line(dealer)}"
		)
		_send_message(dealer, reply, related_type="General")
		return {"message": reply, "quotation_id": ""}

	reply = _quotation_message(frappe.get_doc("Quotation", quotation["id"]))
	_send_message(dealer, reply, related_type="Quotation", related_reference=quotation["id"])
	return {"message": reply, "quotation_id": quotation["id"]}


def _accept_whatsapp_quotation(dealer: str, quotation_id: str, po_number: str) -> dict:
	"""The dealer confirmed the quotation shown in the chat and gave their PO number:
	builds the Sales Order from that quotation. Same credit-limit gate the direct
	WhatsApp order already had (BRD C.11 trigger #1 -- over the limit queues for
	Management, here with the quotation's exact total rather than an estimate), and the
	same rule that nothing raw from ERPNext reaches a dealer."""
	from frappe.utils import add_days, getdate, today

	from dms_erp.sales.credit_limit import gate_credit_limit
	from dms_erp.sales.quotation_api import _convert_to_order
	from dms_erp.sales.utils import safe_customer_message

	def reply(text, related_type="General", reference=None):
		_send_message(dealer, text, related_type=related_type, related_reference=reference)
		return {"message": text}

	# Existence first -- a mistyped/stale id is routine, not an error (same as _find_dealer_order).
	doc = frappe.get_doc("Quotation", quotation_id) if frappe.db.exists("Quotation", quotation_id) else None
	if not doc or doc.party_name != dealer or doc.docstatus != 1:
		return reply("We couldn't find that quotation on your account. Please start again from the main menu.")
	if doc.status not in ("Open", "Replied"):
		return reply(f"Quotation {doc.name} is {doc.status} and can't be confirmed. {dealer_contact_line(dealer)}")
	if doc.valid_till and getdate(doc.valid_till) < getdate(today()):
		return reply(f"Quotation {doc.name} has expired. Please start again from the main menu for a fresh one.")

	try:
		result = gate_credit_limit(
			dealer=dealer,
			additional_value=doc.grand_total,
			reference_doctype="Sales Order",
			applier_action="convert_to_order",
			create_fn=_convert_to_order,
			create_kwargs={
				"quotation": doc.name,
				"expected_dispatch": add_days(today(), 7),
				"customer_po": po_number,
				"ignore_permissions": True,
			},
		)
	except (frappe.PermissionError, frappe.ValidationError) as e:
		return reply(safe_customer_message(e, dealer))

	if isinstance(result, dict) and result.get("approvalRequired"):
		return reply(
			f"Your order against PO {po_number} is over your approved credit limit, so it needs "
			"a quick review by our team before we can confirm it. We'll notify you once it's approved."
		)

	return reply(
		f"Order {result['number']} has been placed against your PO {po_number} from quotation {doc.name}. "
		"We'll confirm dispatch shortly.",
		related_type="Order",
		reference=result["number"],
	)


@frappe.whitelist()
def create_custom_style_inquiry(lead=None, **kwargs):
	"""Flow event `custom_style.create` -- the new "🎨 Custom Style" menu option
	(MOM 3.6 / ISS-2026-00636): "for designs not in the item master, dealers can
	select a Custom Style option and type the item name, creating an inquiry
	record without requiring immediate item creation in the inventory master."
	Explicitly NOT an ERPNext Opportunity, in the bot or the backend -- a Custom
	Style request has no real item behind it at all, so it's logged the same way
	every other WhatsApp inquiry is: a genuine Inquiry (sales.inquiry_api.
	_create_inquiry's new `requested_style` param, Opportunity-mirror skipped for
	exactly this reason -- see that function's own docstring), not a second,
	parallel "thing a dealer asked for" concept.

	lead.message is the style/design name the dealer typed after tapping Custom
	Style -- this Flow node is reached by its own menu branch (not get_item_info's
	lookup step), so there's no earlier resolved item to confuse this with.

	This dealer never gets an order_item_code set as a side effect of this branch
	(unlike a real catalog lookup via get_item_info), so a dealer who tapped
	"🛒 Place Order" straight after a Custom Style request falls through to
	create_dealer_opportunity's own existing "we couldn't tell which item this
	enquiry is for" reply, unchanged -- there is nothing to order yet, by
	construction, and nothing new needed here to guarantee that."""
	phone, text = _lead_fields(lead)
	dealer = dealer_for_phone(phone)
	if not dealer:
		return {"message": "We couldn't find a dealer account for this WhatsApp number. Please contact support."}
	if not text:
		return {"message": "Please describe the style or design you're looking for."}

	_log_inbound_message(dealer, text, related_type="General")

	from dms_erp.sales.inquiry_api import _create_inquiry

	try:
		inquiry = _create_inquiry(dealer=dealer, item=None, qty=1, source="WhatsApp", requested_style=text)
	except (frappe.PermissionError, frappe.ValidationError):
		reply = f"We couldn't log this request against your account. {dealer_contact_line(dealer)}"
		_send_message(dealer, reply, related_type="General")
		return {"message": reply}

	reply = "Thanks — we've noted your custom style request and our team will follow up shortly."
	_send_message(dealer, reply, related_type="Inquiry", related_reference=inquiry["id"])
	return {"message": reply}
