"""ISS-2026-00711 -- client's final decision, 5 Nov 2026: an open Sales Order
that isn't dispatched within the reservation window is now auto-cancelled, not
just flagged. This supersedes ISS-2026-00635's original "flag for review, never
auto-cancel" rule outright -- the client explicitly reversed that call, so this
module no longer has a review-only terminal state at all. (BRD C.3.1 itself
never settled this either way: "Stock reservation review/release -- automatic vs
approval-based release" is listed as an open point, not a confirmed rule.)

BRD C.3.1 is the why stock is reserved at all: reserved on confirmation, "but
long-duration commitments are reviewed and can be released so material is not
blocked idle". Releasing now happens automatically once the window runs out,
instead of waiting on a person to act on a flag.

Day counting starts at the Sales Order's transaction_date. The reminder goes out
once the order is `reminder_day` days old; the order is auto-cancelled once it is
older than `max_days`, i.e. the full window has run out. Both numbers are DMS
Sales Settings, not constants -- this app is white-labeled, and the exact window
is Pacific's own call, not a universal one.

"Fulfilled" means the order has left the warehouse: Dispatched or Delivered.
Cancelled orders (including ones this job already auto-cancelled) are closed
too. Anything else (Confirmed, Picking, Ready to Dispatch) is still holding
stock and counts -- and by construction never has a Delivery Note yet (only
_create_delivery_note, on the Dispatched transition, ever creates one), so
there's no fulfillment progress an auto-cancel at this point could clobber.

custom_reservation_reminder_sent_on is written once and never cleared, so an
order is reminded at most once. custom_reservation_flagged_on now records when
the order was auto-cancelled (kept under its original fieldname -- renaming a
live custom field needs a data-migration patch for no behavioural gain; see
sales/setup.py's label for the field's current, accurate meaning). Who hears
about it: the dealer's own salesperson (Customer.custom_salesperson) for the
reminder, since they're the one who can chase it; Management as well for the
auto-cancel notice, since that's a real outcome on the order, not just a nudge.
A dealer with no salesperson falls back to Management for both. Same in-app
Notification Log the other daily jobs here use -- this app has no outbound staff
email/WhatsApp to send through.
"""

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, today

DEFAULT_MAX_DAYS = 14
DEFAULT_REMINDER_DAY = 7
CLOSED_STAGES = {"Dispatched", "Delivered", "Cancelled"}
REVIEW_ROLES = ["DMS Management"]


def reservation_days() -> tuple[int, int]:
	max_days = frappe.db.get_single_value("DMS Sales Settings", "reservation_max_days") or DEFAULT_MAX_DAYS
	reminder_day = frappe.db.get_single_value("DMS Sales Settings", "reservation_reminder_day") or DEFAULT_REMINDER_DAY
	# A reminder on or after the deadline would never be seen before the flag.
	if reminder_day >= max_days:
		reminder_day = max_days - 1
	return max_days, reminder_day


def reservation_summary(doc, as_of=None) -> dict:
	"""The timeline as the staff app shows it on an order. `state` is one of
	Active / Reminder Sent / Closed.

	There's no "Flagged for Review" state any more (ISS-2026-00711) -- the order's
	own custom_fulfillment_stage flips to Cancelled in the same job run that sets
	flaggedOn, so by the time a caller sees flaggedOn set, the stage check above it
	has already caught it as Closed. flaggedOn is still returned (now meaning "when
	this order was auto-cancelled") for anything that wants to show that date."""
	as_of = getdate(as_of) if as_of else getdate(today())
	max_days, _reminder_day = reservation_days()
	reserved_until = add_days(getdate(doc.transaction_date), max_days)
	flagged_on = doc.get("custom_reservation_flagged_on")
	reminder_sent_on = doc.get("custom_reservation_reminder_sent_on")
	if doc.get("custom_fulfillment_stage") in CLOSED_STAGES or doc.get("docstatus") == 2:
		state = "Closed"
	elif reminder_sent_on:
		state = "Reminder Sent"
	else:
		state = "Active"
	return {
		"reservedUntil": reserved_until,
		"daysLeft": date_diff(reserved_until, as_of),
		"reminderSentOn": reminder_sent_on,
		"flaggedOn": flagged_on,
		"state": state,
	}


def _review_users() -> list[str]:
	from dms_erp.purchase.reorder_api import _users_with_any_role

	return _users_with_any_role(REVIEW_ROLES)


def _salesperson(dealer: str) -> str | None:
	user = frappe.db.get_value("Customer", dealer, "custom_salesperson")
	if user and frappe.db.get_value("User", user, "enabled"):
		return user
	return None


def _notify(users, order: str, subject: str) -> int:
	for user in users:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"document_type": "Sales Order",
				"document_name": order,
				"subject": subject,
			}
		).insert(ignore_permissions=True)
	return len(users)


def review_reservations(as_of=None) -> dict:
	"""Daily. Reminds on orders that reached the reminder day, and auto-cancels
	orders past the window (ISS-2026-00711). An order already past the window on
	its first run (e.g. the day this ships) is cancelled straight away with no
	separate reminder -- a reminder about a deadline that has already passed
	isn't one.

	The `custom_fulfillment_stage not in CLOSED_STAGES` filter already excludes
	anything this job cancelled on an earlier run (Cancelled is a CLOSED_STAGES
	member) -- the extra `custom_reservation_flagged_on is not set` filter is
	belt-and-suspenders against double-processing the same order inside one call,
	not load-bearing on its own."""
	as_of = getdate(as_of) if as_of else getdate(today())
	max_days, reminder_day = reservation_days()
	reminded, cancelled, notified = [], [], 0
	review_users = None

	for row in frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": 1,
			"custom_fulfillment_stage": ["not in", list(CLOSED_STAGES)],
			"custom_reservation_flagged_on": ["is", "not set"],
		},
		fields=["name", "customer", "transaction_date", "custom_reservation_reminder_sent_on"],
	):
		age = date_diff(as_of, row.transaction_date)
		salesperson = _salesperson(row.customer)
		if review_users is None:
			review_users = _review_users()

		if age > max_days:
			from dms_erp.sales.order_api import _cancel_order_action

			_cancel_order_action(
				row.name,
				note=f"Auto-cancelled -- not dispatched within the {max_days}-day reservation window.",
			)
			frappe.db.set_value("Sales Order", row.name, "custom_reservation_flagged_on", as_of, update_modified=False)
			cancelled.append(row.name)
			recipients = set(review_users) | ({salesperson} if salesperson else set())
			notified += _notify(
				recipients,
				row.name,
				_("{0} for {1} was auto-cancelled -- not dispatched within its {2}-day reservation window.").format(
					row.name, row.customer, max_days
				),
			)
		elif age >= reminder_day and not row.custom_reservation_reminder_sent_on:
			frappe.db.set_value(
				"Sales Order", row.name, "custom_reservation_reminder_sent_on", as_of, update_modified=False
			)
			reminded.append(row.name)
			notified += _notify(
				[salesperson] if salesperson else review_users,
				row.name,
				_("{0} for {1}: will be auto-cancelled on {2} ({3} day(s) left) if not dispatched.").format(
					row.name,
					row.customer,
					frappe.format(add_days(row.transaction_date, max_days), {"fieldtype": "Date"}),
					max_days - age,
				),
			)

	return {"reminded": reminded, "cancelled": cancelled, "notified": notified}
