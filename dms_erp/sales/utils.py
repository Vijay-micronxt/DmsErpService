"""Shared helpers used across the sales module — currently just the duplicate-
inquiry predicate, split out so create_inquiry's same-moment check and
reports.duplicate_inquiry_report's after-the-fact list agree on what "still open"
and "the same dealer+item" mean, and on the default window, instead of drifting
apart as two copies of the same logic.
"""

from frappe.utils import add_days, getdate

DUPLICATE_INQUIRY_WINDOW_DAYS = 7

# Same "not yet resolved" set duplicate_inquiry_report already used before this
# existed — a Converted/Rejected/Mapped/Closed inquiry is history, not a live
# duplicate risk.
CLOSED_INQUIRY_STATUSES = {"Converted to Order", "Rejected", "Mapped to PO", "Closed"}


def find_open_duplicate_inquiries(
	dealer: str, item: str, window_days: int = DUPLICATE_INQUIRY_WINDOW_DAYS, as_of=None
) -> list[dict]:
	"""BRD C.2.4 — still-open inquiries for the same dealer+item, logged within
	`window_days` before `as_of` (today by default). Newest first. Used by
	create_inquiry (checked against the moment of creation, before the new
	inquiry itself exists to match against) as a same-moment prompt, not a hard
	block — the caller decides whether to warn or let it through."""
	from dms_erp.sales.inquiry_api import list_all_inquiries

	as_of = getdate(as_of) if as_of else getdate()
	since = add_days(as_of, -window_days)
	matches = [
		i
		for i in list_all_inquiries(dealer=dealer)
		if i["productId"] == item and i["status"] not in CLOSED_INQUIRY_STATUSES and i["date"] and since <= getdate(i["date"]) <= as_of
	]
	matches.sort(key=lambda i: i["date"], reverse=True)
	return matches
