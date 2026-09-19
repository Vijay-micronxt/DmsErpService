"""Product withdrawal automation (BRD C.10.5) — the daily scheduled half of the gap
the backlog tracks in two separate pieces. (a) Whether to remap the live 5-state
lifecycle (Active/Partially Discontinued/Factory Discontinued/Display Removal
Pending/Pulled Back — catalog.utils.DISCONTINUATION_STATUSES) to the BRD's own
4-state model is a data migration that touches the frontend too, and needs
Pacific's sign-off first — not attempted here. This module is (b): the job that
actually evaluates withdrawal signals and steps status forward within the existing
5 states, on the same daily-cron shape as purchase.reorder_api.notify_reorder_review
and pricing.dealer_classification.recompute_dealer_classifications.

BRD C.10.5 names display-duration, store-count and annual-sales as the three
withdrawal signals but leaves the concrete numeric thresholds — and even which
document each signal reads from — to setup-time configuration. There's no single
DMS-wide number that fits every series (a slow-moving premium marble range and a
fast-moving budget range don't share one shelf-life rule), so all three thresholds
live per Series (see the "Product Withdrawal Automation" section on the Series
doctype) and a Series only opts in by checking withdrawal_automation_enabled AND
setting all three thresholds to a positive value — anything else (box unchecked, or
any threshold left at 0) means this job never touches that series' items.

Two of the three signals are proxies forced by what this build actually has:
- "display duration" reads as days since the item's last Delivered sale — the
  closest live signal to "how long has this been sitting without moving". The
  BRD's own display-placement duration only becomes literal once the Sample &
  Display Management module (BRD C.10, still unbuilt — see the backlog) exists.
- "store count" reads as the number of distinct dealers whose Dealer Catalog
  currently lists the item — again a stand-in for an actual store/display count,
  which that same still-unbuilt module would track directly.
"annual sales" is the one real signal: trailing-12-month boxes summed across
Delivered Sales Order Items.

Deliberately conservative in one more way: this job only ever steps an item forward
one stage, Active -> Partially Discontinued -> Factory Discontinued -> Display
Removal Pending. It never sets Pulled Back — that fully blocks a sale outright, and
this app leaves that call to a human, not a nightly job.
"""

import frappe
from frappe.utils import add_days, getdate, today

FORWARD_STEPS = {
	"Active": "Partially Discontinued",
	"Partially Discontinued": "Factory Discontinued",
	"Factory Discontinued": "Display Removal Pending",
}


def _series_with_thresholds() -> dict[str, "frappe._dict"]:
	"""Only a Series with the automation checkbox on AND all three thresholds set to
	a positive value opts in — checked in Python rather than as a DB filter, since an
	unset Int field and an intentionally-blank one aren't reliably distinguishable at
	the DB level, and a 0/unset threshold must never be read as "always breached"."""
	rows = frappe.get_all(
		"Product Series",
		fields=[
			"name",
			"withdrawal_no_sale_days_threshold",
			"withdrawal_min_store_count",
			"withdrawal_min_annual_sales_boxes",
		],
		filters={"withdrawal_automation_enabled": 1},
	)
	return {
		r.name: r
		for r in rows
		if (r.withdrawal_no_sale_days_threshold or 0) > 0
		and (r.withdrawal_min_store_count or 0) > 0
		and (r.withdrawal_min_annual_sales_boxes or 0) > 0
	}


def _days_since_last_sale(item_code: str) -> int | None:
	last_sale = frappe.db.sql(
		"""
		select max(so.transaction_date)
		from `tabSales Order Item` soi
		join `tabSales Order` so on so.name = soi.parent
		where soi.item_code = %s and so.docstatus = 1 and so.custom_fulfillment_stage = 'Delivered'
		""",
		(item_code,),
	)[0][0]
	if not last_sale:
		return None
	return (getdate(today()) - getdate(last_sale)).days


def _store_count(item_code: str) -> int:
	return (
		frappe.db.sql(
			"select count(distinct dci.parent) from `tabDealer Catalog Item` dci where dci.item = %s",
			(item_code,),
		)[0][0]
		or 0
	)


def _annual_sales_boxes(item_code: str) -> float:
	since = add_days(today(), -365)
	total = frappe.db.sql(
		"""
		select sum(soi.qty)
		from `tabSales Order Item` soi
		join `tabSales Order` so on so.name = soi.parent
		where soi.item_code = %s and so.docstatus = 1
		  and so.custom_fulfillment_stage = 'Delivered' and so.transaction_date >= %s
		""",
		(item_code, since),
	)[0][0]
	return total or 0


def evaluate_product_withdrawals() -> int:
	"""Daily scheduled job. For every Item on an opted-in Series that's still in a
	reviewable status (anything before Pulled Back), steps its status forward one
	stage the moment all three signals breach that Series' thresholds at once — an
	item still selling briskly, or one with plenty of store presence, or one that
	simply hasn't been idle long enough, is left alone. Returns the number of items
	actually moved, so callers/tests don't have to re-query to check whether
	anything happened."""
	series_thresholds = _series_with_thresholds()
	if not series_thresholds:
		return 0

	items = frappe.get_all(
		"Item",
		fields=["name", "custom_discontinuation_status", "custom_series_ref"],
		filters={
			"custom_series_ref": ["in", list(series_thresholds)],
			"custom_discontinuation_status": ["in", list(FORWARD_STEPS)],
		},
	)

	moved = 0
	for item in items:
		thresholds = series_thresholds[item.custom_series_ref]

		days_idle = _days_since_last_sale(item.name)
		if days_idle is None or days_idle < thresholds.withdrawal_no_sale_days_threshold:
			continue
		if _store_count(item.name) >= thresholds.withdrawal_min_store_count:
			continue
		if _annual_sales_boxes(item.name) >= thresholds.withdrawal_min_annual_sales_boxes:
			continue

		next_status = FORWARD_STEPS.get(item.custom_discontinuation_status or "Active")
		if not next_status:
			continue
		frappe.db.set_value("Item", item.name, "custom_discontinuation_status", next_status)
		moved += 1
	return moved
