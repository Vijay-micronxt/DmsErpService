"""Catalog-side reports (BRD "Reports and Dashboards"). Read-only over the
existing pricing/reorder listings — see sales_reports.py's module docstring for
the report-vs-dashboard distinction this whole `reports` module follows.
"""

import frappe

from dms_erp.catalog.api import list_all_products
from dms_erp.pricing.api import get_price_record, list_all_price_records
from dms_erp.purchase.reorder_api import sales_velocity_by_item


@frappe.whitelist(methods=["GET"])
def pricing_and_csp_report(min_margin_pct: float | None = None):
	"""The BRD's "Pricing and CSP report" (CSP = Customer Suggested Price — the
	approved dealer price itself, the base figure a Quotation's markup is applied
	on top of). Only approved price records are meaningful here; a Pending one has
	no CSP to report yet."""
	rows = []
	for record in list_all_price_records():
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


@frappe.whitelist(methods=["GET"])
def product_activity_report(item: str | None = None):
	"""The BRD's "Product activity report" — a per-item rollup across four
	modules (Inquiry, Sales Order, Stock Entry transfers, price-approval history)
	that no single existing list/get function crosses on its own."""
	products = list_all_products()
	if item:
		products = [p for p in products if p["id"] == item]

	rows = []
	for p in products:
		code = p["id"]
		inquiry_count = frappe.db.count("Inquiry", {"item": code})
		order_qty = (
			frappe.db.sql(
				"""
				select coalesce(sum(soi.qty), 0)
				from `tabSales Order Item` soi
				inner join `tabSales Order` so on so.name = soi.parent
				where soi.item_code = %s and so.docstatus = 1
				""",
				(code,),
			)[0][0]
			or 0
		)
		transfer_count = (
			frappe.db.sql(
				"""
				select count(distinct sed.parent)
				from `tabStock Entry Detail` sed
				inner join `tabStock Entry` se on se.name = sed.parent
				where sed.item_code = %s and se.docstatus = 1 and se.purpose = 'Material Transfer'
				""",
				(code,),
			)[0][0]
			or 0
		)
		price_record = get_price_record(code)
		price_changes = len(price_record["history"]) if price_record else 0

		rows.append(
			{
				"productId": code,
				"itemName": p["name"],
				"category": p["category"],
				"inquiryCount": inquiry_count,
				"orderQty": order_qty,
				"transferCount": transfer_count,
				"priceChanges": price_changes,
			}
		)

	rows.sort(key=lambda r: r["orderQty"], reverse=True)
	return rows
