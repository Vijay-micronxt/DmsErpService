"""Bay-to-bay material transfer — a native ERPNext Stock Entry (Material Transfer),
batch-aware via s_warehouse/t_warehouse, not a custom doctype. Only the handful of
Pacific-specific attributes (transfer type/reason, damage type, claim ref) are
Custom Fields on Stock Entry (see warehouse/setup.py).
"""

import frappe
from frappe import _

from dms_erp.warehouse.bay_api import BAY_WRITE_ROLES
from dms_erp.warehouse.utils import bay_occupancy, default_company, get_bay, list_stock_lots


def _assert_can_transfer():
	if not set(frappe.get_roles(frappe.session.user)) & BAY_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can move stock between bays."), frappe.PermissionError)


def _serialize(doc) -> dict:
	row = doc.items[0]
	return {
		"id": doc.name,
		"ref": doc.name,
		"itemCode": row.item_code,
		"batchNumber": row.batch_no,
		"fromBayId": row.s_warehouse,
		"toBayId": row.t_warehouse,
		"qty": row.qty,
		"transferType": doc.custom_transfer_type,
		"reason": doc.custom_transfer_reason,
		"damageType": doc.custom_damage_type,
		"claimRef": doc.custom_claim_ref,
		"remarks": doc.custom_remarks,
		"transferredAt": doc.posting_date,
		"transferredBy": doc.owner,
	}


@frappe.whitelist(methods=["GET"])
def list_transfers():
	names = frappe.get_all(
		"Stock Entry", filters={"purpose": "Material Transfer", "docstatus": 1, "custom_transfer_type": ["!=", ""]}, pluck="name", order_by="creation desc"
	)
	return [_serialize(frappe.get_doc("Stock Entry", name)) for name in names]


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
