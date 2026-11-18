"""Catalog-side reports (BRD "Reports and Dashboards"). Read-only over the
existing pricing/reorder listings — see sales_reports.py's module docstring for
the report-vs-dashboard distinction this whole `reports` module follows.
"""

import frappe

from dms_erp.pricing.api import list_all_price_records
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
	items = frappe.get_all("Item", fields=["name", "item_name", "item_group"], order_by="item_code asc")
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
	that no single existing list/get function crosses on its own.

	Four grouped queries for the whole catalog, not four per item plus a full product
	serialization per item: on the ~4,300 migrated items that was 15,000+ queries and
	timed out. Same figures, same row order (stable sort over item_code order)."""
	filters = {"name": item} if item else {}
	products = frappe.get_all(
		"Item", filters=filters, fields=["name", "item_name", "item_group"], order_by="item_code asc"
	)

	inquiries = dict(
		frappe.db.sql(
			"""select item, count(*) from `tabInquiry Item`
			where parenttype = 'Inquiry' and item is not null group by item"""
		)
	)
	# 12 Nov 2026 rebuild -- item moved to the Inquiry Item child table.
	ordered = dict(
		frappe.db.sql(
			"""select soi.item_code, coalesce(sum(soi.qty), 0)
			from `tabSales Order Item` soi
			inner join `tabSales Order` so on so.name = soi.parent
			where so.docstatus = 1 group by soi.item_code"""
		)
	)
	transfers = dict(
		frappe.db.sql(
			"""select sed.item_code, count(distinct sed.parent)
			from `tabStock Entry Detail` sed
			inner join `tabStock Entry` se on se.name = sed.parent
			where se.docstatus = 1 and se.purpose = 'Material Transfer' group by sed.item_code"""
		)
	)
	price_changes = dict(
		frappe.db.sql(
			"""select parent, count(*) from `tabItem Price Proposal History`
			where parenttype = 'Item Price Proposal' group by parent"""
		)
	)

	rows = [
		{
			"productId": p.name,
			"itemName": p.item_name,
			"category": p.item_group,
			"inquiryCount": inquiries.get(p.name, 0),
			"orderQty": ordered.get(p.name) or 0,
			"transferCount": transfers.get(p.name, 0),
			"priceChanges": price_changes.get(p.name, 0),
		}
		for p in products
	]
	rows.sort(key=lambda r: r["orderQty"], reverse=True)
	return rows
