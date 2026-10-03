"""Reorder Plan (BRD C.4.1) — "the Reorder Plan doctype and its review screen are
custom... the team reviews the auto-generated plan and adjusts quantity... before
creating POs... responsible members are notified to review it."

reorder_api.reorder_suggestions() is a live, per-request computation with nothing
to open, adjust or audit later. This module persists a snapshot of it as a real
document: one Reorder Plan per generation, one Reorder Plan Item row per item with
a suggestedQty > 0 at that moment. Staff adjust `adjusted_qty` on a line (the
engine's own `suggested_qty` is frozen, never overwritten, so the review screen can
show what changed), mark the plan Reviewed, then raise POs referencing it —
po_api.create_purchase_order's own `reorder_plan` param tags each line a resulting
PO actually covers.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from dms_erp.pagination import clamp

REORDER_PLAN_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}


def _assert_can_manage_reorder_plans():
	if not set(frappe.get_roles(frappe.session.user)) & REORDER_PLAN_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage reorder plans."), frappe.PermissionError)


def _serialize_item(row) -> dict:
	return {
		"id": row.name,
		"item": row.item,
		"defaultSupplier": row.default_supplier,
		"urgency": row.urgency,
		"moq": row.moq or 0,
		"currentStock": row.current_stock or 0,
		"missedDemandQty": row.missed_demand_qty or 0,
		"pendingInquiryQty": row.pending_inquiry_qty or 0,
		"recentRetailSalesQty": row.recent_retail_sales_qty or 0,
		"openPurchaseOrderQty": row.open_po_qty or 0,
		"reasons": row.reasons,
		"suggestedQty": row.suggested_qty or 0,
		"adjustedQty": row.adjusted_qty or 0,
		"purchaseOrder": row.purchase_order,
	}


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"generatedOn": doc.generated_on,
		"status": doc.status,
		"reviewedBy": doc.reviewed_by,
		"reviewedAt": doc.reviewed_at,
		"items": [_serialize_item(row) for row in doc.items],
	}


@frappe.whitelist(methods=["POST"])
def generate_reorder_plan(suggestions: list[dict] | None = None) -> dict:
	"""`suggestions` lets notify_reorder_review (which already computed them once
	to check whether anything is pending) hand them over instead of paying for a
	second reorder_suggestions() run; the scheduler and any manual trigger from the
	UI always call this with no argument and let it compute them itself."""
	_assert_can_manage_reorder_plans()

	if suggestions is None:
		from dms_erp.purchase.reorder_api import reorder_suggestions

		suggestions = reorder_suggestions()

	pending = [s for s in suggestions if s["suggestedQty"] > 0]

	plan = frappe.get_doc(
		{
			"doctype": "Reorder Plan",
			"generated_on": now_datetime(),
			"status": "Draft",
			"items": [
				{
					"item": s["productId"],
					"default_supplier": s["defaultSupplier"],
					"urgency": s["urgency"],
					"moq": s.get("moq", 0),
					"current_stock": s["currentStock"],
					"missed_demand_qty": s["missedDemandQty"],
					"pending_inquiry_qty": s["pendingInquiryQty"],
					"recent_retail_sales_qty": s["recentRetailSalesQty"],
					"open_po_qty": s["openPurchaseOrderQty"],
					"reasons": "; ".join(s["reasons"]),
					"suggested_qty": s["suggestedQty"],
					"adjusted_qty": s["suggestedQty"],
				}
				for s in pending
			],
		}
	)
	plan.insert(ignore_permissions=True)
	return _serialize(plan)


@frappe.whitelist(methods=["GET"])
def list_reorder_plans(limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	total = frappe.db.count("Reorder Plan")
	names = frappe.get_all(
		"Reorder Plan", pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Reorder Plan", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_reorder_plan(plan: str):
	return _serialize(frappe.get_doc("Reorder Plan", plan))


@frappe.whitelist(methods=["POST", "PUT"])
def update_plan_item_qty(plan: str, item_row: str, adjusted_qty: float):
	_assert_can_manage_reorder_plans()

	doc = frappe.get_doc("Reorder Plan", plan)
	if doc.status == "Closed":
		frappe.throw(_("This plan is closed and can no longer be adjusted."), frappe.ValidationError)

	row = next((r for r in doc.items if r.name == item_row), None)
	if not row:
		frappe.throw(_("No such line on this Reorder Plan."), frappe.DoesNotExistError)
	if adjusted_qty is None or float(adjusted_qty) < 0:
		frappe.throw(_("Adjusted quantity cannot be negative."), frappe.ValidationError)

	row.adjusted_qty = adjusted_qty
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def mark_plan_reviewed(plan: str):
	_assert_can_manage_reorder_plans()

	doc = frappe.get_doc("Reorder Plan", plan)
	if doc.status != "Draft":
		frappe.throw(_("Only a Draft plan can be marked Reviewed."), frappe.ValidationError)

	doc.status = "Reviewed"
	doc.reviewed_by = frappe.session.user
	doc.reviewed_at = now_datetime()
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def close_plan(plan: str):
	_assert_can_manage_reorder_plans()

	doc = frappe.get_doc("Reorder Plan", plan)
	if doc.status == "Closed":
		frappe.throw(_("This plan is already closed."), frappe.ValidationError)

	doc.status = "Closed"
	doc.save(ignore_permissions=True)
	return _serialize(doc)
