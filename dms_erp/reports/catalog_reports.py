"""Catalog-side reports (BRD "Reports and Dashboards"). Read-only over the
existing pricing/reorder listings — see sales_reports.py's module docstring for
the report-vs-dashboard distinction this whole `reports` module follows.
"""

import frappe

from dms_erp.pricing.api import list_price_records
from dms_erp.purchase.reorder_api import sales_velocity_by_item


@frappe.whitelist(methods=["GET"])
def pricing_and_csp_report(min_margin_pct: float | None = None):
	"""The BRD's "Pricing and CSP report" (CSP = Customer Suggested Price — the
	approved dealer price itself, the base figure a Quotation's markup is applied
	on top of). Only approved price records are meaningful here; a Pending one has
	no CSP to report yet."""
	rows = []
	for record in list_price_records():
		if record["status"] != "Approved" or not record["landingCost"]:
			continue
		csp = record["suggestedPrice"]
		actual_margin_pct = round((csp - record["landingCost"]) / csp * 100, 2) if csp else 0
		row = {
			"productId": record["productId"],
			"landingCost": record["landingCost"],
			"targetMarginPct": record["marginPct"],
			"csp": csp,
			"actualMarginPct": actual_margin_pct,
		}
		if min_margin_pct is None or actual_margin_pct >= min_margin_pct:
			rows.append(row)

	rows.sort(key=lambda r: r["actualMarginPct"])
	return rows


@frappe.whitelist(methods=["GET"])
def product_movement_report(order: str = "slow"):
	"""The BRD's "Fast/slow-moving product report" — every item ranked by the same
	trailing-window Retail sales velocity the reorder engine already computes
	per-item, here exposed across the whole catalog at once."""
	if order not in ("slow", "fast"):
		frappe.throw(f"Invalid order: {order}")

	velocity = sales_velocity_by_item()
	items = frappe.get_all("Item", fields=["name", "item_name", "item_group"])
	rows = [
		{"productId": i.name, "itemName": i.item_name, "category": i.item_group, "recentSalesQty": velocity.get(i.name, 0)}
		for i in items
	]
	rows.sort(key=lambda r: r["recentSalesQty"], reverse=(order == "fast"))
	return rows
