"""Dealer inactivity monitoring (MOM 07-Oct-2026 §3.7 / action item #6): "A 3-month
inactivity threshold will be applied to dealer accounts. Inactive dealers will be
flagged and Prashant Sir will be notified. Every month, the system will generate a
list of active dealer numbers to support WhatsApp Business compliance."

"Activity" is the latest of four things a dealer can actually do: a Sales Order, a
Sales Invoice, an Inquiry, or an inbound WhatsApp message. Sales Invoice matters
even though this app never posts one live -- it's where the whole Tally history
lives, so without it every dealer whose last purchase predates go-live would read
as inactive on day one. Customer.creation is only the fallback for a dealer with
no activity at all (a dealer onboarded yesterday isn't "inactive", they're new);
it is deliberately not a floor under real activity -- a live check on the
migrated site found 265 of 394 dealers dormant >90 days, which a creation-date
floor would have hidden for the first three months, since migration created every
Customer on the same day.

Computed per dealer in four GROUP BY queries rather than per-dealer lookups -- this
runs over every Customer daily, and the Tally migration alone brought ~17k Sales
Invoices across a few hundred dealers.

The flag (Customer.custom_inactive) is informational only: nothing is blocked,
disabled or hidden by it, and it clears itself the next run after a dealer is
active again. Only the transition *into* inactive notifies anyone -- re-notifying
the same dormant dealers every single day would just be noise.

Who's notified is DMS Sales Settings.inactive_dealer_notify_user (the MOM names
Prashant Sir specifically) or, left blank, every enabled DMS Management user -- a
named person is per-client config in this white-labeled app, never hardcoded. Same
in-app Notification Log alert the reorder digest (purchase.reorder_api) already
uses, rather than email/WhatsApp to staff, which nothing else here sends either.

The monthly "active dealer numbers" list is active_dealer_numbers (whitelisted,
on demand) plus notify_active_dealer_numbers (monthly scheduler), which tells the
same recipient the list is ready and how many numbers are on it. This app can't
itself submit anything to Meta -- what WhatsApp Business compliance actually does
with the list is a manual/middleware step outside dms_erp.
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

DEFAULT_INACTIVITY_DAYS = 90
NOTIFY_FALLBACK_ROLES = ["DMS Management"]
_SUBJECT_NAME_LIMIT = 5


def inactivity_days() -> int:
	return frappe.db.get_single_value("DMS Sales Settings", "dealer_inactivity_days") or DEFAULT_INACTIVITY_DAYS


def _grouped_max(query: str) -> dict:
	return {dealer: getdate(day) for dealer, day in frappe.db.sql(query) if dealer and day}


def last_activity_by_dealer() -> dict:
	sources = [
		"select customer, max(transaction_date) from `tabSales Order` where docstatus < 2 group by customer",
		"select customer, max(posting_date) from `tabSales Invoice` where docstatus = 1 group by customer",
		"select dealer, max(`date`) from `tabInquiry` group by dealer",
		"select dealer, max(date(sent_at)) from `tabWhatsApp Message` where direction = 'Inbound' group by dealer",
	]
	latest: dict = {}
	for query in sources:
		for dealer, day in _grouped_max(query).items():
			if dealer not in latest or day > latest[dealer]:
				latest[dealer] = day
	return latest


def _notify_users() -> list[str]:
	user = frappe.db.get_single_value("DMS Sales Settings", "inactive_dealer_notify_user")
	if user and frappe.db.get_value("User", user, "enabled"):
		return [user]
	from dms_erp.purchase.reorder_api import _users_with_any_role

	return _users_with_any_role(NOTIFY_FALLBACK_ROLES)


def _notify(subject: str) -> int:
	users = _notify_users()
	for user in users:
		frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": "Alert", "subject": subject}).insert(
			ignore_permissions=True
		)
	return len(users)


def flag_inactive_dealers(as_of=None) -> dict:
	"""Daily. Recomputes custom_last_activity_on/custom_inactive for every enabled
	dealer and notifies on dealers that newly crossed the threshold. Returns counts
	so callers (and tests) needn't re-query Customer to see what it did."""
	as_of = getdate(as_of) if as_of else getdate(today())
	cutoff = add_days(as_of, -inactivity_days())
	latest = last_activity_by_dealer()

	newly_inactive: list[str] = []
	inactive_total = 0
	for row in frappe.get_all(
		"Customer",
		filters={"disabled": 0},
		fields=["name", "customer_name", "creation", "custom_last_activity_on", "custom_inactive"],
	):
		# Real activity wins outright. Customer.creation is only the fallback for a
		# dealer with none at all -- never a floor under real activity: the Tally
		# migration created every Customer on migration day, so flooring on it would
		# make every long-dormant dealer look freshly active for a full 90 days.
		last = latest.get(row.name) or getdate(row.creation)
		inactive = last < cutoff
		inactive_total += inactive

		updates = {}
		if row.custom_last_activity_on is None or getdate(row.custom_last_activity_on) != last:
			updates["custom_last_activity_on"] = last
		if bool(row.custom_inactive) != inactive:
			updates["custom_inactive"] = int(inactive)
			if inactive:
				newly_inactive.append(row.customer_name or row.name)
		if updates:
			frappe.db.set_value("Customer", row.name, updates, update_modified=False)

	notified = 0
	if newly_inactive:
		shown = ", ".join(newly_inactive[:_SUBJECT_NAME_LIMIT])
		more = len(newly_inactive) - _SUBJECT_NAME_LIMIT
		if more > 0:
			shown += _(" and {0} more").format(more)
		notified = _notify(
			_("{0} dealer(s) newly inactive (no activity in {1} days): {2}").format(
				len(newly_inactive), inactivity_days(), shown
			)
		)
	return {"newlyInactive": len(newly_inactive), "inactiveTotal": inactive_total, "notified": notified}


@frappe.whitelist(methods=["GET"])
def active_dealer_numbers() -> list[dict]:
	"""MOM §3.7 -- "a list of active dealer numbers to support WhatsApp Business
	compliance." Every enabled dealer not currently flagged inactive, with a phone
	on file. Reads the stored flag rather than recomputing, so it matches exactly
	what flag_inactive_dealers last decided."""
	rows = frappe.get_all(
		"Customer",
		filters={"disabled": 0, "custom_inactive": 0, "custom_phone": ["is", "set"]},
		fields=["name", "customer_name", "custom_phone", "custom_last_activity_on"],
		order_by="customer_name asc",
	)
	return [
		{
			"dealerId": r.name,
			"dealerName": r.customer_name,
			"phone": r.custom_phone,
			"lastActivityOn": r.custom_last_activity_on,
		}
		for r in rows
	]


def notify_active_dealer_numbers() -> int:
	"""Monthly. Tells the same recipient flag_inactive_dealers notifies that this
	month's active-number list is ready, and how long it is."""
	count = len(active_dealer_numbers())
	return _notify(_("Monthly active dealer numbers: {0} active dealer(s) with a phone on file.").format(count))
