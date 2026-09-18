"""Loose / unallocated stock (BRD C.6.6) — a tile or box set down outside an
allocated bay. Genuinely untracked until this exists: nothing else in this app
posts anything to Stock Ledger for it (unlike allocation_api.create_allocation,
which posts a real Purchase Receipt), so recording one here is a pure log entry,
not a stock movement.

From Pending, an entry resolves one of two ways:
- allocate_unallocated_stock: the warehouse decides it belongs in a proper bay —
  this is the actual stock-effecting event, going through the same
  allocation_api.create_allocation every other inbound lot does.
- consolidate_unallocated_stock: the warehouse decides it's the same lot as
  existing open/partial stock elsewhere and merges it there by hand — no new
  stock movement, just a status/remarks update recording that decision.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from dms_erp.pagination import clamp
from dms_erp.warehouse.bay_api import BAY_WRITE_ROLES


def _assert_can_manage_unallocated_stock():
	if not set(frappe.get_roles(frappe.session.user)) & BAY_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage unallocated stock."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"item": doc.item,
		"batchNumber": doc.batch_no,
		"qty": doc.qty,
		"location": doc.location,
		"status": doc.status,
		"scannedBy": doc.scanned_by,
		"scannedAt": doc.scanned_at,
		"resolvedAllocation": doc.resolved_allocation,
		"remarks": doc.remarks,
	}


@frappe.whitelist(methods=["GET"])
def list_unallocated_stock(status: str | None = "Pending", search: str | None = None, limit: int = 20, offset: int = 0):
	"""Doubles as BRD C.6.6's "unallocated-stock check/report" — defaults to just
	Pending (the periodic-check use case); pass status=None for every entry
	regardless of how it was resolved."""
	limit, offset = clamp(limit, offset)
	filters = {}
	if status:
		filters["status"] = status
	if search:
		filters["location"] = ["like", f"%{search}%"]
	total = frappe.db.count("Unallocated Stock Entry", filters=filters)
	names = frappe.get_all(
		"Unallocated Stock Entry", filters=filters, pluck="name", order_by="scanned_at asc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Unallocated Stock Entry", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_unallocated_stock_entry(entry: str):
	return _serialize(frappe.get_doc("Unallocated Stock Entry", entry))


@frappe.whitelist(methods=["POST"])
def record_unallocated_stock(item: str, qty: float, location: str, batch_no: str | None = None):
	"""BRD C.6.6: scan item + ad-hoc location from the mobile scanning interface."""
	_assert_can_manage_unallocated_stock()

	if not location or not location.strip():
		frappe.throw(_("An ad-hoc location description is required."), frappe.ValidationError)

	doc = frappe.get_doc(
		{
			"doctype": "Unallocated Stock Entry",
			"item": item,
			"batch_no": batch_no,
			"qty": qty,
			"location": location.strip(),
			"status": "Pending",
			"scanned_by": frappe.session.user,
			"scanned_at": now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def allocate_unallocated_stock(entry: str, bay: str, supplier: str):
	"""Resolves a Pending entry into a real Bay Allocation (the actual
	stock-effecting event) — same shape as any other inbound lot allocation."""
	_assert_can_manage_unallocated_stock()

	from dms_erp.warehouse.allocation_api import create_allocation

	doc = frappe.get_doc("Unallocated Stock Entry", entry)
	if doc.status != "Pending":
		frappe.throw(_("Only a Pending entry can be allocated."), frappe.ValidationError)

	batch_no = doc.batch_no or f"UNALLOC-{doc.name}"
	allocation = create_allocation(
		item=doc.item, batch_no=batch_no, total_qty=doc.qty, lines=[{"bay": bay, "qty": doc.qty}], supplier=supplier
	)

	doc.status = "Allocated"
	doc.resolved_allocation = allocation["id"]
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST"])
def consolidate_unallocated_stock(entry: str, remarks: str):
	"""Warehouse decided this is the same lot as existing open/partial stock
	elsewhere and merged it there by hand — no new stock movement here, only the
	record of that decision (BRD C.6.6)."""
	_assert_can_manage_unallocated_stock()

	if not remarks or not remarks.strip():
		frappe.throw(_("A remark describing what this was consolidated into is required."), frappe.ValidationError)

	doc = frappe.get_doc("Unallocated Stock Entry", entry)
	if doc.status != "Pending":
		frappe.throw(_("Only a Pending entry can be consolidated."), frappe.ValidationError)

	doc.status = "Consolidated"
	doc.remarks = remarks.strip()
	doc.save(ignore_permissions=True)
	return _serialize(doc)
