"""Sales-side reports (BRD "Reports and Dashboards"). Every report here reads
through the existing sales-module list/get functions rather than re-querying the
underlying doctypes directly — the date-range/grouping logic that makes a listing
into a "report" lives here; the data access itself stays owned by sales/inquiry_api.py
and sales/quotation_api.py.

Unlike Phase 8's Dashboard endpoints (a fixed KPI snapshot for one role's home
screen), reports here take filters (dealer, status, date range) and return rows
meant for a report screen — no role gate on the read itself, matching how every
other list/get endpoint in this app works; only writes are role-gated.
"""

import frappe
from frappe.utils import getdate

from dms_erp.comms.api import last_message
from dms_erp.pricing.api import get_dealer_price
from dms_erp.purchase.reorder_api import MISSED_DEMAND_STATUSES
from dms_erp.sales import dealer_api, inquiry_api

DUPLICATE_INQUIRY_WINDOW_DAYS = 7
# Inquiry's 10-state lifecycle (sales/inquiry_api.py) — these four are the ones
# that mean the demand has already been actioned or dropped; everything else is
# still "open" and eligible to be flagged as a duplicate.
CLOSED_INQUIRY_STATUSES = {"Converted to Order", "Rejected", "Mapped to PO", "Closed"}


def _in_range(d, from_date, to_date) -> bool:
	if not d:
		return from_date is None and to_date is None
	d = getdate(d)
	if from_date and d < getdate(from_date):
		return False
	if to_date and d > getdate(to_date):
		return False
	return True


@frappe.whitelist(methods=["GET"])
def dealer_inquiry_report(dealer: str | None = None, status: str | None = None, from_date=None, to_date=None):
	"""Every inquiry for a dealer (or across all dealers), with a status breakdown —
	the BRD's "Dealer inquiry report"."""
	rows = [r for r in inquiry_api.list_inquiries(dealer=dealer, status=status) if _in_range(r["date"], from_date, to_date)]

	by_status: dict[str, int] = {}
	for r in rows:
		by_status[r["status"]] = by_status.get(r["status"], 0) + 1

	return {"rows": rows, "summary": {"total": len(rows), "byStatus": by_status}}


@frappe.whitelist(methods=["GET"])
def missed_demand_report(from_date=None, to_date=None):
	"""Every inquiry that current stock couldn't satisfy (Out of Stock / Pre-order
	Required), each priced at the approved dealer price — the row-level version of
	the Phase 8 sales dashboard's single missedDemandValue number."""
	rows = []
	total_value = 0
	for r in inquiry_api.list_inquiries():
		if r["status"] not in MISSED_DEMAND_STATUSES or not _in_range(r["date"], from_date, to_date):
			continue
		price = get_dealer_price(r["productId"]) or 0
		value = r["qty"] * price
		total_value += value
		rows.append({**r, "estimatedValue": value})

	rows.sort(key=lambda r: r["estimatedValue"], reverse=True)
	return {"rows": rows, "totalValue": total_value}


@frappe.whitelist(methods=["GET"])
def retail_vs_bulk_report(from_date=None, to_date=None):
	"""The BRD's "Retail vs bulk report" — order count and value by
	`Sales Order.custom_order_channel` (Phase 15), over an optional date range.
	Unblocked entirely by Phase 15; before that field existed there was nothing
	to group by."""
	conditions = ["so.docstatus = 1"]
	values: dict = {}
	if from_date:
		conditions.append("so.transaction_date >= %(from_date)s")
		values["from_date"] = from_date
	if to_date:
		conditions.append("so.transaction_date <= %(to_date)s")
		values["to_date"] = to_date

	rows = frappe.db.sql(
		f"""
		select so.custom_order_channel as channel, count(so.name) as order_count, coalesce(sum(so.grand_total), 0) as value
		from `tabSales Order` so
		where {' and '.join(conditions)}
		group by so.custom_order_channel
		""",
		values,
		as_dict=True,
	)
	return {"byChannel": [{"channel": r.channel, "orderCount": r.order_count, "value": r.value} for r in rows]}


@frappe.whitelist(methods=["GET"])
def dealer_activity_report(dealer: str | None = None):
	"""The BRD's "Dealer activity report" — a per-dealer rollup across four modules
	(Inquiry, Quotation, Sales Order, WhatsApp Message) that no single existing
	list/get function crosses on its own, unlike every other report in this file."""
	dealers = [dealer_api.get_dealer(dealer)] if dealer else dealer_api.list_dealers()

	rows = []
	for d in dealers:
		did = d["id"]
		inquiry_count = frappe.db.count("Inquiry", {"dealer": did})
		quotation_count = frappe.db.count("Quotation", {"party_name": did, "quotation_to": "Customer", "docstatus": ["!=", 2]})
		order_agg = frappe.db.sql(
			"select count(name) as cnt, coalesce(sum(grand_total), 0) as value from `tabSales Order` where customer=%s and docstatus=1",
			(did,),
			as_dict=True,
		)[0]
		message_count = frappe.db.count("WhatsApp Message", {"dealer": did})
		last = last_message(did)

		rows.append(
			{
				"dealerId": did,
				"dealerName": d["name"],
				"inquiryCount": inquiry_count,
				"quotationCount": quotation_count,
				"orderCount": order_agg.cnt,
				"orderValue": order_agg.value,
				"messageCount": message_count,
				"lastContact": last["sentAt"] if last else None,
			}
		)

	rows.sort(key=lambda r: r["orderValue"], reverse=True)
	return rows


@frappe.whitelist(methods=["GET"])
def duplicate_inquiry_report(window_days: int = DUPLICATE_INQUIRY_WINDOW_DAYS):
	"""The BRD's "Duplicate inquiry report". No duplicate rule was specified in
	the BRD text, so this is a proposed one, easy to retune via `window_days`:
	two or more still-open inquiries (not yet Converted to Order / Mapped to PO /
	Rejected / Closed) for the same dealer and item, all logged within
	`window_days` of each other."""
	open_inquiries = [i for i in inquiry_api.list_inquiries() if i["status"] not in CLOSED_INQUIRY_STATUSES]

	groups: dict[tuple, list] = {}
	for i in open_inquiries:
		groups.setdefault((i["dealerId"], i["productId"]), []).append(i)

	out = []
	for (dealer, product), group in groups.items():
		if len(group) < 2:
			continue
		dates = sorted(getdate(i["date"]) for i in group if i["date"])
		if not dates or (dates[-1] - dates[0]).days > window_days:
			continue
		out.append(
			{
				"dealerId": dealer,
				"productId": product,
				"count": len(group),
				"inquiries": [{"id": i["id"], "date": i["date"], "qty": i["qty"], "status": i["status"]} for i in group],
			}
		)

	out.sort(key=lambda r: r["count"], reverse=True)
	return out
