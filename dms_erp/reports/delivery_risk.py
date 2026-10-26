"""BRD C.3.2's forecasting/stock window (BRD "Reports and Dashboards"). Own module
rather than folded into `purchase_reports.py` or `sales_reports.py` — its data
genuinely spans both (a Sales Order's own delivery_date, a Purchase Order's own
schedule_date and the Inward Truck/Bin state between them), unlike any existing
report in either of those files. Read-only over `sales.delivery_risk.
at_risk_sales_order_lines`, the same domain module the daily escalation job
(`review_delivery_risk`) uses, so the report and the flag agree — see
`reports/README.md`'s own "every report reads through the source module's own
function" rule.

This is a live FIFO *model* of which open order is jeopardized by already-delayed
supply, not a recorded allocation — nothing in the real fulfillment process
actually assigns a specific incoming PO line to a specific outgoing order. See
`sales/delivery_risk.py`'s own module docstring for why, and `forecasting.py` for
the same "modeled, not recorded" honesty applied elsewhere in this module.
"""

import frappe

from dms_erp.sales.delivery_risk import at_risk_sales_order_lines


def _rollup(rows: list[dict], key: str) -> dict:
	counts: dict[str, int] = {}
	for row in rows:
		if key == "supplier":
			for po in row["coveringPoLines"]:
				counts[po["supplier"]] = counts.get(po["supplier"], 0) + (1 if row["atRisk"] else 0)
			continue
		value = row[key]
		counts[value] = counts.get(value, 0) + (1 if row["atRisk"] else 0)
	return counts


@frappe.whitelist(methods=["GET"])
def delivery_risk_window(customer: str | None = None, item: str | None = None, supplier: str | None = None):
	"""BRD C.3.2's forecasting/stock window — per order and item, stock-in-hand,
	in-transit and factory-dispatch status, with the FIFO-modeled at-risk flag.
	Filterable by any one of customer/item/supplier; the three summary rollups
	(at-risk line counts by each axis) let the same flat row set answer "groupable
	by customer, item or supplier" without three separate endpoints, matching how
	`po_pending_report`'s own filter params work."""
	rows = at_risk_sales_order_lines()
	if customer:
		rows = [r for r in rows if r["customer"] == customer]
	if item:
		rows = [r for r in rows if r["item"] == item]
	if supplier:
		rows = [r for r in rows if supplier in {po["supplier"] for po in r["coveringPoLines"]}]

	return {
		"rows": rows,
		"summary": {
			"lineCount": len(rows),
			"atRiskCount": sum(1 for r in rows if r["atRisk"]),
			"byCustomer": _rollup(rows, "customer"),
			"byItem": _rollup(rows, "item"),
			"bySupplier": _rollup(rows, "supplier"),
		},
	}
