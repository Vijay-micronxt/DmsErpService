"""Purchase-side reports (BRD "Reports and Dashboards"). Read-only over the
existing PO/reorder listings — see sales_reports.py's module docstring for the
report-vs-dashboard distinction this whole `reports` module follows.
"""

import frappe

from dms_erp.purchase import po_api
from dms_erp.purchase.reorder_api import reorder_suggestions
from dms_erp.sales import inquiry_api


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


@frappe.whitelist(methods=["GET"])
def inquiry_to_po_mapping_report():
	"""The BRD's "Inquiry-to-PO mapping report" — every PO raised via `sales.
	inquiry_api.convert_to_purchase_requirement`, joined back to the Inquiry that
	drove it. Purchase Orders raised directly (no source_inquiry) don't appear
	here — there's nothing to map."""
	po_names = frappe.get_all("Purchase Order", filters={"custom_source_inquiry": ["is", "set"], "docstatus": 1}, pluck="name")

	rows = []
	for po_name in po_names:
		po = po_api.get_purchase_order(po_name)
		inquiry = inquiry_api.get_inquiry(po["sourceInquiry"])
		rows.append(
			{
				"inquiryId": inquiry["id"],
				"dealerId": inquiry["dealerId"],
				"productId": inquiry["productId"],
				"inquiryQty": inquiry["qty"],
				"inquiryStatus": inquiry["status"],
				"poId": po["id"],
				"poSupplier": po["supplier"],
				"poOrderedQty": sum(l["orderedQty"] for l in po["lines"]),
				"poReceivedQty": sum(l["receivedQty"] for l in po["lines"]),
			}
		)
	return rows


@frappe.whitelist(methods=["GET"])
def po_pending_report(supplier: str | None = None, overdue_only: bool = False):
	"""The BRD's "PO pending report" — wraps `po_api.list_pending_po_lines` with
	the supplier/overdue-only filters a purchase planning report wants."""
	rows = po_api.list_pending_po_lines()
	if supplier:
		rows = [r for r in rows if r["supplier"] == supplier]
	if overdue_only:
		rows = [r for r in rows if r["daysOverdue"] > 0]

	return {
		"rows": rows,
		"summary": {
			"lineCount": len(rows),
			"totalPendingQty": sum(r["pendingQty"] for r in rows),
			"overdueCount": sum(1 for r in rows if r["daysOverdue"] > 0),
		},
	}
