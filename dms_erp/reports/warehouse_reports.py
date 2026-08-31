"""Warehouse-side reports (BRD "Reports and Dashboards"). Read-only over the
existing bay/stock listings — see sales_reports.py's module docstring for the
report-vs-dashboard distinction this whole `reports` module follows.

Stock Clearance Suggestion and Display Replacement Suggestion have no
BRD-specified rule — the BRD names the report, not the formula. Both are
proposed heuristics, deliberately built from small, named, easy-to-retune
constants rather than magic numbers buried in the query, so the actual
thresholds can be corrected without touching the logic around them.
"""

import frappe
from frappe.utils import getdate, today

from dms_erp.catalog.utils import is_sellable
from dms_erp.purchase.reorder_api import SAFETY_STOCK_BOXES, SALES_VELOCITY_WINDOW_DAYS, sales_velocity_by_item
from dms_erp.warehouse.bay_api import list_bays
from dms_erp.warehouse.utils import list_stock_lots, total_stock_for_item

DISPLAY_BAY_TYPE = "display"
CLEARANCE_STOCK_MULTIPLE = 2  # currentStock beyond this many safety-stock floors is "well above"
CLEARANCE_MIN_AGE_DAYS = 90


@frappe.whitelist(methods=["GET"])
def bay_occupancy_report(warehouse: str | None = None, bay_type: str | None = None):
	"""Every bay's occupancy — the BRD's "Bay occupancy report". `list_bays` already
	returns occupiedBoxes/occupancyPct/freeBoxes per bay; this just adds the
	warehouse/type filters and a fleet-wide summary a report screen wants."""
	rows = list_bays()
	if warehouse:
		rows = [r for r in rows if r["warehouse"] == warehouse]
	if bay_type:
		rows = [r for r in rows if r["type"] == bay_type]

	total_capacity = sum(r["capacityBoxes"] or 0 for r in rows)
	total_occupied = sum(r["occupiedBoxes"] or 0 for r in rows)
	return {
		"rows": rows,
		"summary": {
			"bayCount": len(rows),
			"avgOccupancyPct": round(total_occupied / total_capacity * 100) if total_capacity else 0,
			"fullBays": sum(1 for r in rows if r["occupancyPct"] >= 95),
		},
	}


@frappe.whitelist(methods=["GET"])
def visual_stock_balance(warehouse: str | None = None):
	"""Every bay, with the lots physically sitting in it — the BRD's "Visual stock
	balance". Shaped for a bay-grid/heatmap UI: the "visual" part is a frontend
	rendering job over data that's otherwise identical to bay_occupancy_report plus
	list_stock grouped by bay."""
	bays = list_bays()
	if warehouse:
		bays = [b for b in bays if b["warehouse"] == warehouse]

	lots_by_bay: dict[str, list] = {}
	for lot in list_stock_lots():
		lots_by_bay.setdefault(lot["bayId"], []).append(lot)

	return [{**bay, "lots": lots_by_bay.get(bay["id"], [])} for bay in bays]


@frappe.whitelist(methods=["GET"])
def stock_clearance_suggestions():
	"""The BRD's "Stock clearance suggestion". Flags an item when all three hold:
	current stock is more than `CLEARANCE_STOCK_MULTIPLE`x the safety-stock floor,
	its trailing-window Retail sales velocity hasn't even covered one floor's
	worth of demand, and its oldest batch is at least `CLEARANCE_MIN_AGE_DAYS`
	old — well-stocked, barely moving, and aging, all at once."""
	velocity = sales_velocity_by_item()

	oldest_by_item: dict = {}
	for lot in list_stock_lots():
		stored_at = getdate(lot["storedAt"])
		if lot["itemCode"] not in oldest_by_item or stored_at < oldest_by_item[lot["itemCode"]]:
			oldest_by_item[lot["itemCode"]] = stored_at

	today_date = getdate(today())
	out = []
	for item_code, oldest in oldest_by_item.items():
		age_days = (today_date - oldest).days
		if age_days < CLEARANCE_MIN_AGE_DAYS:
			continue

		stock = total_stock_for_item(item_code)
		if stock <= SAFETY_STOCK_BOXES * CLEARANCE_STOCK_MULTIPLE:
			continue

		recent_sales = velocity.get(item_code, 0)
		if recent_sales >= SAFETY_STOCK_BOXES:
			continue

		item_doc = frappe.get_cached_doc("Item", item_code)
		out.append(
			{
				"productId": item_code,
				"itemName": item_doc.item_name,
				"currentStock": stock,
				"recentSalesQty": recent_sales,
				"oldestBatchAgeDays": age_days,
				"reason": (
					f"{stock} boxes on hand vs. a {SAFETY_STOCK_BOXES}-box safety floor, only {recent_sales} sold in "
					f"the last {SALES_VELOCITY_WINDOW_DAYS} days, oldest batch {age_days} days old"
				),
			}
		)

	out.sort(key=lambda r: r["oldestBatchAgeDays"], reverse=True)
	return out


@frappe.whitelist(methods=["GET"])
def display_replacement_suggestions():
	"""The BRD's "Display replacement suggestion". For every item currently
	occupying a display-type bay that's either not selling (zero trailing-window
	Retail sales) or past Active in the discontinuation lifecycle, suggests the
	fastest-moving currently-sellable item in the same category that isn't
	already on display."""
	display_bay_ids = {b["id"] for b in list_bays() if b["type"] == DISPLAY_BAY_TYPE}
	if not display_bay_ids:
		return []

	velocity = sales_velocity_by_item()
	displayed_lots = [l for l in list_stock_lots() if l["bayId"] in display_bay_ids]
	displayed_items = {l["itemCode"] for l in displayed_lots}

	candidates_by_category: dict[str, list] = {}
	for item_code, qty in velocity.items():
		if item_code in displayed_items or qty <= 0:
			continue
		item_doc = frappe.get_cached_doc("Item", item_code)
		status = item_doc.custom_discontinuation_status or "Active"
		if not is_sellable(status):
			continue
		candidates_by_category.setdefault(item_doc.item_group, []).append((qty, item_code, item_doc.item_name))
	for candidates in candidates_by_category.values():
		candidates.sort(key=lambda c: c[0], reverse=True)

	out = []
	seen_items = set()
	for lot in displayed_lots:
		item_code = lot["itemCode"]
		if item_code in seen_items:
			continue
		seen_items.add(item_code)

		item_doc = frappe.get_cached_doc("Item", item_code)
		status = item_doc.custom_discontinuation_status or "Active"
		is_slow = velocity.get(item_code, 0) == 0
		is_discontinuing = status != "Active"
		if not (is_slow or is_discontinuing):
			continue

		reasons = []
		if is_discontinuing:
			reasons.append(f"status is {status}")
		if is_slow:
			reasons.append(f"no Retail sales in the last {SALES_VELOCITY_WINDOW_DAYS} days")

		best = next(iter(candidates_by_category.get(item_doc.item_group, [])), None)
		out.append(
			{
				"currentDisplayItem": item_code,
				"itemName": item_doc.item_name,
				"bayId": lot["bayId"],
				"reason": " and ".join(reasons),
				"suggestedReplacement": best[1] if best else None,
				"replacementReason": (
					f"fastest-moving {item_doc.item_group} not currently displayed ({best[0]} boxes sold recently)"
					if best
					else f"no faster-moving, currently-sellable {item_doc.item_group} candidate found"
				),
			}
		)
	return out
