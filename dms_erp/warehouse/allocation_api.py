"""Bay Allocation (BRD §10.4-10.5) and scan-confirmed put-away (§11).

Confirming an allocation is the actual stock-effecting event: it posts a native
Purchase Receipt (one item row per bay split) so Bin/Stock Ledger Entry — not a
custom table — become the source of truth for what's on hand where. Scan-confirmed
put-away happens *after* that (the floor check that material actually landed where
the slip said) and does not move stock again; it just closes out the truck/slip.

Linking the received Purchase Receipt back to a Purchase Order is deliberately not
attempted yet — that needs Phase 4's real PO/reorder flow to exist first. Inward
Truck still carries `purchase_order`/`purchase_order_item` as informational links.
"""

import frappe
from frappe import _
from frappe.utils import today

from dms_erp.pricing.api import get_price_record
from dms_erp.warehouse.bay_api import BAY_WRITE_ROLES
from dms_erp.warehouse.utils import default_company, ensure_batch, get_bay, list_stock_lots, validate_allocation


def _assert_can_allocate():
	if not set(frappe.get_roles(frappe.session.user)) & BAY_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can allocate bays."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"slipNumber": doc.name,
		"inwardTruck": doc.inward_truck,
		"purchaseOrder": doc.purchase_order,
		"itemCode": doc.item,
		"batchNumber": doc.batch_no,
		"totalQty": doc.total_qty,
		"status": doc.status,
		"purchaseReceipt": doc.purchase_receipt,
		"allocations": [
			{"bayId": row.bay, "bayCode": frappe.db.get_value("Warehouse", row.bay, "custom_bay_code"), "qty": row.qty, "confirmed": bool(row.confirmed)}
			for row in doc.lines
		],
		"createdAt": doc.creation,
	}


@frappe.whitelist(methods=["POST"])
def create_allocation(
	item: str,
	batch_no: str,
	total_qty: float,
	lines: list[dict],
	inward_truck: str | None = None,
	supplier: str | None = None,
):
	_assert_can_allocate()

	if not lines:
		frappe.throw(_("At least one bay allocation line is required."), frappe.ValidationError)

	line_total = sum(float(l["qty"]) for l in lines)
	if abs(line_total - float(total_qty)) > 0.01:
		frappe.throw(_("Allocation lines ({0}) must add up to the total quantity ({1}).").format(line_total, total_qty), frappe.ValidationError)

	item_group = frappe.get_cached_value("Item", item, "item_group")
	resolved_lines = []
	for line in lines:
		# `bay` is always the human bay code (e.g. "A-01"), never the Warehouse doc's
		# own (company-abbreviation-suffixed) name — resolve it once, up front.
		bay = get_bay(line["bay"])
		errors = [i for i in validate_allocation(bay.custom_bay_code, float(line["qty"]), item_group) if i["level"] == "error"]
		if errors:
			frappe.throw(errors[0]["message"], frappe.ValidationError)
		resolved_lines.append({"bay_name": bay.name, "qty": float(line["qty"])})

	truck = frappe.get_doc("Inward Truck", inward_truck) if inward_truck else None
	supplier = supplier or (truck.supplier if truck else None)
	if not supplier:
		frappe.throw(_("A supplier is required (directly, or via inward_truck)."), frappe.ValidationError)

	ensure_batch(item, batch_no)

	alloc = frappe.get_doc(
		{
			"doctype": "Bay Allocation",
			"inward_truck": inward_truck,
			"item": item,
			"batch_no": batch_no,
			"total_qty": total_qty,
			"status": "Confirmed",
			"lines": [{"bay": l["bay_name"], "qty": l["qty"], "confirmed": 1} for l in resolved_lines],
		}
	)
	alloc.insert(ignore_permissions=True)

	price_record = get_price_record(item)
	rate = (price_record or {}).get("purchaseCost") or 0

	pr = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"supplier": supplier,
			"company": default_company(),
			"posting_date": today(),
			"items": [
				{"item_code": item, "qty": l["qty"], "warehouse": l["bay_name"], "batch_no": batch_no, "rate": rate}
				for l in resolved_lines
			],
		}
	)
	pr.insert(ignore_permissions=True)
	pr.submit()

	alloc.purchase_receipt = pr.name
	alloc.save(ignore_permissions=True)

	if truck:
		truck.batch_no = batch_no
		truck.allocation_slip = alloc.name
		truck.save(ignore_permissions=True)

	return _serialize(alloc)


@frappe.whitelist(methods=["POST", "PUT"])
def mark_allocation_printed(allocation: str):
	_assert_can_allocate()
	doc = frappe.get_doc("Bay Allocation", allocation)
	doc.status = "Printed"
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["GET"])
def resolve_scan(code: str):
	"""Read-only lookup mirroring the frontend's PI-BAY|/PI-ITEM|-prefixed codes, plus
	bare bay-code / item-code / batch-number manual entry."""
	code = (code or "").strip()
	if not code:
		return {"ok": False, "kind": "unknown", "message": "Enter or scan a code."}
	upper = code.upper()

	if upper.startswith("PI-BAY|"):
		bay_code = code[len("PI-BAY|"):]
		return _resolve_bay_code(bay_code)

	if upper.startswith("PI-ITEM|"):
		parts = code.split("|")
		item_code = parts[1] if len(parts) > 1 else ""
		batch = parts[2] if len(parts) > 2 else ""
		bay_code = parts[3] if len(parts) > 3 else None
		return _resolve_lot(item_code, batch, bay_code)

	bay_name = frappe.db.get_value("Warehouse", {"custom_bay_code": upper}, "name")
	if bay_name:
		return _resolve_bay_code(upper)

	lots = [l for l in list_stock_lots() if l["itemCode"].upper() == upper or l["batchNumber"].upper() == upper]
	if lots:
		lot = lots[0]
		return {"ok": True, "kind": "item", "lot": lot, "message": f"{lot['itemCode']} · {lot['batchNumber']} — {lot['boxes']} boxes in {lot['bayId']}"}

	return {"ok": False, "kind": "unknown", "message": f'No match for "{code}".'}


def _resolve_bay_code(bay_code: str):
	name = frappe.db.get_value("Warehouse", {"custom_bay_code": bay_code.upper()}, "name")
	if not name:
		return {"ok": False, "kind": "unknown", "message": f'No bay found for code "{bay_code}".'}
	bay = frappe.get_doc("Warehouse", name)
	return {"ok": True, "kind": "bay", "bay": {"id": bay.name, "code": bay.custom_bay_code, "type": bay.custom_bay_type}, "message": f"{bay.custom_bay_code} — {bay.custom_bay_type}"}


def _resolve_lot(item_code: str, batch: str, bay_code: str | None):
	lots = [
		l
		for l in list_stock_lots()
		if l["itemCode"].upper() == item_code.upper()
		and l["batchNumber"].upper() == batch.upper()
		and (not bay_code or frappe.get_cached_value("Warehouse", l["bayId"], "custom_bay_code") == bay_code.upper())
	]
	if not lots:
		return {"ok": False, "kind": "unknown", "message": f"No stock found for {item_code} · {batch}."}
	lot = lots[0]
	return {"ok": True, "kind": "item", "lot": lot, "message": f"{lot['itemCode']} · {lot['batchNumber']} — {lot['boxes']} boxes in {lot['bayId']}"}


@frappe.whitelist(methods=["POST"])
def confirm_putaway(allocation: str):
	"""Floor confirmation only — stock was already posted when the allocation was
	created (see create_allocation)."""
	_assert_can_allocate()

	doc = frappe.get_doc("Bay Allocation", allocation)
	doc.status = "Placed"
	doc.save(ignore_permissions=True)

	if doc.inward_truck:
		truck = frappe.get_doc("Inward Truck", doc.inward_truck)
		truck.status = "Put-away"
		truck.save(ignore_permissions=True)

	return _serialize(doc)
