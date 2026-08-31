"""Forecasting (BRD "Reports and Dashboards" — "Forecasting dashboard").

No forecasting methodology was specified in the BRD text — the BRD names the
report, not the formula, same as three of Phase 19's reports. Deliberately kept
to the simplest defensible method rather than guessing at something more
elaborate: a trailing `FORECAST_HISTORY_WEEKS`-week average of Retail sales
velocity, projected flat across the requested horizon. It does NOT model
seasonality (e.g. a wedding-season demand spike) or trend — every row is
flagged "low" confidence for exactly that reason, so a caller can't mistake this
for more than it is. Swap the method here once a real methodology is confirmed
(seasonal decomposition, a longer history window, category-level pooling for
sparse items, etc.) — nothing else in the `reports` module depends on how this
one is computed.
"""

import frappe
from frappe.utils import add_days, today

FORECAST_HISTORY_WEEKS = 12


@frappe.whitelist(methods=["GET"])
def demand_forecast(weeks_ahead: int = 4):
	if weeks_ahead <= 0:
		frappe.throw("weeks_ahead must be a positive number of weeks.")

	since = add_days(today(), -FORECAST_HISTORY_WEEKS * 7)
	rows = frappe.db.sql(
		"""
		select soi.item_code as item, sum(soi.qty) as qty
		from `tabSales Order Item` soi
		inner join `tabSales Order` so on so.name = soi.parent
		where so.docstatus = 1 and so.transaction_date >= %s and so.custom_order_channel = 'Retail'
		group by soi.item_code
		""",
		(since,),
		as_dict=True,
	)

	out = []
	for row in rows:
		avg_weekly_qty = round(row.qty / FORECAST_HISTORY_WEEKS, 1)
		item_doc = frappe.get_cached_doc("Item", row.item)
		out.append(
			{
				"productId": row.item,
				"itemName": item_doc.item_name,
				"avgWeeklyQty": avg_weekly_qty,
				"projectedQty": round(avg_weekly_qty * weeks_ahead),
				"weeksAhead": weeks_ahead,
				"method": f"trailing-{FORECAST_HISTORY_WEEKS}-week average",
				"confidence": "low — no seasonality or trend modeled",
			}
		)

	out.sort(key=lambda r: r["projectedQty"], reverse=True)
	return out
