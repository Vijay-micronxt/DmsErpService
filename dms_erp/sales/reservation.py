"""ISS-2026-00635 -- "On Sales Order, add reservation timeline logic. Max buffer: 21
days (7+7+7). Auto-send reminder notification at day 14+6 (day 20). If order is not
fulfilled within 21 days, flag for review. Do not auto-cancel -- flag only."

BRD C.3.1 is the why: stock is reserved on confirmation, "but long-duration
commitments are reviewed and can be released so material is not blocked idle".
This module is the "reviewed" half only. Releasing stays a person's decision
(cancel or progress the order as usual) -- the ticket rules out auto-cancel.

Day counting starts at the Sales Order's transaction_date. The reminder goes out
once the order is `reminder_day` days old (20); the flag is set once it is older
than `max_days` (21), i.e. the full 21-day window has run out. Both numbers are
DMS Sales Settings, not constants -- 7+7+7 is Pacific's rule, and this app is
white-labeled.

"Fulfilled" means the order has left the warehouse: Dispatched or Delivered.
Cancelled orders are closed too. Anything else (Confirmed, Picking, Ready to
Dispatch) is still holding stock and counts.

The two dates set here (custom_reservation_reminder_sent_on/_flagged_on) are
written once and never cleared, so each order is reminded and flagged at most
once, and the order keeps a record of both even after it ships. Who hears about
it: the dealer's own salesperson (Customer.custom_salesperson) for the reminder,
since they're the one who can chase it; Management as well for the flag, since
"review" is their call. A dealer with no salesperson falls back to Management for
both. Same in-app Notification Log the other daily jobs here use -- this app has
no outbound staff email/WhatsApp to send through.
"""

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, today

DEFAULT_MAX_DAYS = 21
DEFAULT_REMINDER_DAY = 20
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
	Active / Reminder Sent / Flagged for Review / Closed."""
	as_of = getdate(as_of) if as_of else getdate(today())
	max_days, _reminder_day = reservation_days()
	reserved_until = add_days(getdate(doc.transaction_date), max_days)
	flagged_on = doc.get("custom_reservation_flagged_on")
	reminder_sent_on = doc.get("custom_reservation_reminder_sent_on")
	if doc.get("custom_fulfillment_stage") in CLOSED_STAGES or doc.get("docstatus") == 2:
		state = "Closed"
	elif flagged_on:
		state = "Flagged for Review"
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
	"""Daily. Reminds on orders that reached the reminder day and flags orders past
	the window. An order already past the window on its first run (e.g. the day
	this ships) is flagged straight away with no separate reminder -- a reminder
	about a deadline that has already passed isn't one."""
	as_of = getdate(as_of) if as_of else getdate(today())
	max_days, reminder_day = reservation_days()
	reminded, flagged, notified = [], [], 0
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
			frappe.db.set_value("Sales Order", row.name, "custom_reservation_flagged_on", as_of, update_modified=False)
			flagged.append(row.name)
			recipients = set(review_users) | ({salesperson} if salesperson else set())
			notified += _notify(
				recipients,
				row.name,
				_("{0} for {1} is past its {2}-day reservation window and not yet dispatched -- review it.").format(
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
				_("{0} for {1}: reservation ends on {2} ({3} day(s) left) and it isn't dispatched yet.").format(
					row.name,
					row.customer,
					frappe.format(add_days(row.transaction_date, max_days), {"fieldtype": "Date"}),
					max_days - age,
				),
			)

	return {"reminded": reminded, "flagged": flagged, "notified": notified}
