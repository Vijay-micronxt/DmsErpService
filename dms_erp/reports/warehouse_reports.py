"""Warehouse-side reports (BRD "Reports and Dashboards"). Read-only over the
existing bay/stock listings — see sales_reports.py's module docstring for the
report-vs-dashboard distinction this whole `reports` module follows.
"""

import frappe

from dms_erp.warehouse.bay_api import list_bays
from dms_erp.warehouse.utils import list_stock_lots


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
