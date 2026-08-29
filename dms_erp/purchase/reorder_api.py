"""Reorder-suggestion engine (BRD §12.3): current stock, missed retail demand, open
retail inquiries, and six months of retail sales velocity.

Phase 9's checklist reconciliation flagged `missedDemandQty`, `pendingInquiryQty` and
`recentRetailSalesQty` as honest zero placeholders — Phase 5 (Inquiries/Orders) data
didn't exist yet when Phase 4 wrote this module. It exists now, so all three are wired
to real queries:

- `pendingInquiryQty` sums `Inquiry.qty` across the still-open statuses of Inquiry's
  10-state lifecycle (Open / Available / Partially Available / Quoted) — demand
  actively in the pipeline, not yet an Order and not yet dropped.
- `missedDemandQty` sums `Inquiry.qty` for Out of Stock / Pre-order Required —
  demand a dealer actually raised that current stock could not satisfy, a stronger
  reorder signal than a still-open inquiry.
- `recentRetailSalesQty` sums submitted Sales Order Item qty over the trailing
  `SALES_VELOCITY_WINDOW_DAYS`, converted to a daily rate and projected across the
  item's own `lead_time_days` (native Item field, already surfaced as `leadTimeDays`
  in Phase 2's catalog API) — the classic reorder-point "expected demand during lead
  time" term, added on top of the flat `SAFETY_STOCK_BOXES` floor rather than
  replacing it.

Phase 13 adds `openPurchaseOrderQty`/`openPurchaseOrders`: suggestions have no
suggestion-id of their own to tag a PO against (a suggestion is a live per-item
computation, not a stored row), so "already ordered" instead means "is there an
open PO for this item, still pending receipt" — netted straight out of `raw_need`
so a fully-covered item's `suggestedQty` drops to 0 without a separate "covered"
flag to keep in sync.
"""

import frappe
from frappe.utils import add_days, today

from dms_erp.catalog.utils import is_reorderable
from dms_erp.warehouse.utils import total_stock_for_item

SAFETY_STOCK_BOXES = 100
SALES_VELOCITY_WINDOW_DAYS = 180

PENDING_INQUIRY_STATUSES = ["Open", "Available", "Partially Available", "Quoted"]
MISSED_DEMAND_STATUSES = ["Out of Stock", "Pre-order Required"]

URGENCY_STYLES_ORDER = ["Critical", "High", "Watch", "Healthy"]


def _grouped_inquiry_qty(statuses: list[str]) -> dict[str, float]:
	rows = frappe.get_all(
		"Inquiry",
		filters={"status": ["in", statuses]},
		fields=["item", "sum(qty) as qty"],
		group_by="item",
	)
	return {r.item: r.qty for r in rows}


def _grouped_recent_sales_qty() -> dict[str, float]:
	since = add_days(today(), -SALES_VELOCITY_WINDOW_DAYS)
	rows = frappe.db.sql(
		"""
		select soi.item_code as item, sum(soi.qty) as qty
		from `tabSales Order Item` soi
		inner join `tabSales Order` so on so.name = soi.parent
		where so.docstatus = 1 and so.transaction_date >= %s
		group by soi.item_code
		""",
		(since,),
		as_dict=True,
	)
	return {r.item: r.qty for r in rows}


def _grouped_open_purchase_orders() -> dict[str, list[dict]]:
	"""Per item, every submitted PO line not yet fully received — docstatus=1 already
	excludes Cancelled; Completed/Closed are excluded explicitly since those are done,
	not open."""
	rows = frappe.db.sql(
		"""
		select poi.item_code as item, po.name as po, (poi.qty - coalesce(poi.received_qty, 0)) as pending_qty
		from `tabPurchase Order Item` poi
		inner join `tabPurchase Order` po on po.name = poi.parent
		where po.docstatus = 1 and po.status not in ('Completed', 'Closed')
		""",
		as_dict=True,
	)
	grouped: dict[str, list[dict]] = {}
	for row in rows:
		if row.pending_qty <= 0:
			continue
		grouped.setdefault(row.item, []).append({"po": row.po, "pendingQty": row.pending_qty})
	return grouped


@frappe.whitelist(methods=["GET"])
def reorder_suggestions():
	items = frappe.get_all("Item", fields=["name", "custom_discontinuation_status", "lead_time_days"])
	missed_by_item = _grouped_inquiry_qty(MISSED_DEMAND_STATUSES)
	pending_by_item = _grouped_inquiry_qty(PENDING_INQUIRY_STATUSES)
	sales_by_item = _grouped_recent_sales_qty()
	open_po_by_item = _grouped_open_purchase_orders()

	suggestions = [
		_suggestion_for(
			item,
			total_stock_for_item(item.name),
			missed_by_item.get(item.name, 0),
			pending_by_item.get(item.name, 0),
			sales_by_item.get(item.name, 0),
			open_po_by_item.get(item.name, []),
		)
		for item in items
	]
	rank = {u: i for i, u in enumerate(URGENCY_STYLES_ORDER)}
	suggestions.sort(key=lambda s: rank[s["urgency"]])
	return suggestions


def _suggestion_for(
	item,
	current_stock: float,
	missed_demand_qty: float,
	pending_inquiry_qty: float,
	recent_retail_sales_qty: float,
	open_purchase_orders: list[dict],
) -> dict:
	status = item.custom_discontinuation_status or "Active"
	non_reorderable = not is_reorderable(status)

	daily_velocity = recent_retail_sales_qty / SALES_VELOCITY_WINDOW_DAYS
	lead_time_demand_qty = round(daily_velocity * (item.lead_time_days or 0))
	open_po_qty = sum(po["pendingQty"] for po in open_purchase_orders)

	reasons = []
	if missed_demand_qty > 0:
		reasons.append(f"{missed_demand_qty} boxes of missed/constrained retail demand")
	if pending_inquiry_qty > 0:
		reasons.append(f"{pending_inquiry_qty} boxes in open retail inquiries")
	if lead_time_demand_qty > 0:
		reasons.append(
			f"~{lead_time_demand_qty} boxes of expected demand across a {item.lead_time_days}-day lead time "
			f"({recent_retail_sales_qty} boxes sold in the last {SALES_VELOCITY_WINDOW_DAYS} days)"
		)
	if current_stock == 0:
		reasons.append("Zero stock on hand")
	elif current_stock < SAFETY_STOCK_BOXES:
		reasons.append(f"Below {SAFETY_STOCK_BOXES}-box safety stock")
	if open_po_qty > 0:
		reasons.append(f"{open_po_qty} boxes already on order across {len(open_purchase_orders)} open PO(s)")

	raw_need = missed_demand_qty + pending_inquiry_qty + lead_time_demand_qty + SAFETY_STOCK_BOXES - current_stock - open_po_qty
	suggested_qty = 0 if non_reorderable else max(0, round(raw_need / 10) * 10)

	urgency = "Healthy"
	if not non_reorderable:
		if current_stock == 0 and missed_demand_qty > 0:
			urgency = "Critical"
		elif suggested_qty > 0 and missed_demand_qty > 0:
			urgency = "High"
		elif suggested_qty > 0:
			urgency = "Watch"
	if non_reorderable and missed_demand_qty > 0:
		reasons.append(f"{status} — demand exists but not reorderable")

	return {
		"productId": item.name,
		"currentStock": current_stock,
		"missedDemandQty": missed_demand_qty,
		"pendingInquiryQty": pending_inquiry_qty,
		"recentRetailSalesQty": recent_retail_sales_qty,
		"openPurchaseOrderQty": open_po_qty,
		"openPurchaseOrders": open_purchase_orders,
		"suggestedQty": suggested_qty,
		"urgency": urgency,
		"reasons": reasons,
		"nonReorderable": non_reorderable,
	}
