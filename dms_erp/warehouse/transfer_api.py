"""Bay-to-bay material transfer — a native ERPNext Stock Entry (Material Transfer),
batch-aware via s_warehouse/t_warehouse, not a custom doctype. Only the handful of
Pacific-specific attributes (transfer type/reason, damage type, claim ref) are
Custom Fields on Stock Entry (see warehouse/setup.py).
"""

import frappe
from frappe import _

from dms_erp.catalog.utils import (
	item_pieces_per_box,
	item_sqft_per_box,
	item_sqm_per_box,
	item_weight_per_box_kg,
)
from dms_erp.pagination import clamp
from dms_erp.warehouse.bay_api import BAY_WRITE_ROLES
from dms_erp.warehouse.utils import bay_occupancy, default_company, get_bay, list_stock_lots


def _assert_can_transfer():
	if not set(frappe.get_roles(frappe.session.user)) & BAY_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can move stock between bays."), frappe.PermissionError)


def _weight_per_box_kg(item: str, batch_no: str | None) -> float | None:
	# Same batch-overrides-item-standard fallback as inward_api/allocation_api
	# (BRD C.1.3/C.6.1) -- a transfer moves a specific batch, so its own weight
	# (once known) is the best figure, not the item's generic standard.
	if batch_no:
		batch_weight = frappe.db.get_value("Batch", batch_no, "custom_batch_weight_kg")
		if batch_weight is not None:
			return batch_weight
	return item_weight_per_box_kg(item)


def _serialize(doc) -> dict:
	row = doc.items[0]
	weight_per_box_kg = _weight_per_box_kg(row.item_code, row.batch_no)
	pieces_per_box = item_pieces_per_box(row.item_code)
	sqft_per_box = item_sqft_per_box(row.item_code)
	sqm_per_box = item_sqm_per_box(row.item_code)
	return {
		"id": doc.name,
		"ref": doc.name,
		"itemCode": row.item_code,
		"batchNumber": row.batch_no,
		"fromBayId": row.s_warehouse,
		"toBayId": row.t_warehouse,
		"qty": row.qty,
		"weightPerBoxKg": weight_per_box_kg,
		"totalWeightKg": (weight_per_box_kg or 0) * row.qty if weight_per_box_kg is not None else None,
		"piecesPerBox": pieces_per_box,
		"totalPieces": (pieces_per_box or 0) * row.qty if pieces_per_box is not None else None,
		"sqftPerBox": sqft_per_box,
		"totalSqft": (sqft_per_box or 0) * row.qty if sqft_per_box is not None else None,
		"sqmPerBox": sqm_per_box,
		"totalSqm": round((sqm_per_box or 0) * row.qty, 4) if sqm_per_box is not None else None,
		"transferType": doc.custom_transfer_type,
		"reason": doc.custom_transfer_reason,
		"damageType": doc.custom_damage_type,
		"claimRef": doc.custom_claim_ref,
		"remarks": doc.custom_remarks,
		"transferredAt": doc.posting_date,
		"transferredBy": doc.owner,
	}


_TRANSFER_FILTERS = {"purpose": "Material Transfer", "docstatus": 1, "custom_transfer_type": ["!=", ""]}


def list_all_transfers() -> list[dict]:
	"""Unpaginated — for internal callers (dashboard) that need the full result set,
	not a page of it. list_transfers (the whitelisted endpoint) is the paginated one."""
	names = frappe.get_all("Stock Entry", filters=_TRANSFER_FILTERS, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Stock Entry", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_transfers(search: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = dict(_TRANSFER_FILTERS)
	if search:
		filters["name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Stock Entry", filters=filters)
	names = frappe.get_all(
		"Stock Entry", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Stock Entry", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["POST"])
def transfer_stock(
	from_bay: str,
	to_bay: str,
	item: str,
	batch_no: str,
	qty: float,
	transfer_type: str,
	reason: str,
	remarks: str | None = None,
	damage_type: str | None = None,
	claim_ref: str | None = None,
):
	_assert_can_transfer()

	if float(qty) <= 0:
		frappe.throw(_("Enter a quantity greater than zero."), frappe.ValidationError)

	source_bay = get_bay(from_bay)
	dest_bay = get_bay(to_bay)

	available = sum(
		l["boxes"] for l in list_stock_lots(bay=source_bay.name, item=item) if l["batchNumber"] == batch_no
	)
	if float(qty) > available:
		frappe.throw(_("Only {0} boxes of this batch in {1}.").format(available, source_bay.custom_bay_code), frappe.ValidationError)

	free = bay_occupancy(dest_bay)["free"]
	if float(qty) > free:
		frappe.throw(_("{0} has only {1} boxes of free capacity.").format(dest_bay.custom_bay_code, free), frappe.ValidationError)

	entry = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"purpose": "Material Transfer",
			"stock_entry_type": "Material Transfer",
			"company": default_company(),
			"custom_transfer_type": transfer_type,
			"custom_transfer_reason": reason,
			"custom_remarks": remarks,
			"custom_damage_type": damage_type,
			"custom_claim_ref": claim_ref,
			"items": [
				{
					"item_code": item,
					"qty": qty,
					"batch_no": batch_no,
					"s_warehouse": source_bay.name,
					"t_warehouse": dest_bay.name,
				}
			],
		}
	)
	entry.insert(ignore_permissions=True)
	entry.submit()

	return _serialize(entry)
