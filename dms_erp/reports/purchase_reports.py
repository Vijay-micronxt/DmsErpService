"""Purchase-side reports (BRD "Reports and Dashboards"). Read-only over the
existing PO/reorder listings — see sales_reports.py's module docstring for the
report-vs-dashboard distinction this whole `reports` module follows.
"""

import frappe

from dms_erp.purchase import po_api
from dms_erp.purchase.reorder_api import reorder_suggestions


@frappe.whitelist(methods=["GET"])
def reorder_planning_report(urgency: str | None = None, actionable_only: bool = False):
	"""The BRD's "Purchase reorder planning report" — the reorder engine's own
	suggestions, already urgency-ranked, filtered for a planning meeting rather
	than a live dashboard tile."""
	rows = reorder_suggestions()
	if urgency:
		rows = [r for r in rows if r["urgency"] == urgency]
	if actionable_only:
		rows = [r for r in rows if r["suggestedQty"] > 0]
	return rows


@frappe.whitelist(methods=["GET"])
def purchase_pickup_plan():
	"""The BRD's "Purchase pickup plan" — supplier-confirmed-ready material not yet
	booked onto a truck. Same data as the Phase 8 purchase dashboard's
	materialsReadyForPickup, exposed as its own filterable report."""
	rows = po_api.list_materials_ready_for_pickup()
	return {"rows": rows, "summary": {"lineCount": len(rows), "totalReadyQty": sum(r["readyQty"] for r in rows)}}
