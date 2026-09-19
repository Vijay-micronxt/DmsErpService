"""Bay Allocation (BRD §10.4-10.5) and scan-confirmed put-away (§11).

Confirming an allocation is the actual stock-effecting event: it posts a native
Purchase Receipt (one item row per bay split) so Bin/Stock Ledger Entry — not a
custom table — become the source of truth for what's on hand where. Scan-confirmed
put-away happens *after* that (the floor check that material actually landed where
the slip said) and does not move stock again; it just closes out the truck/slip.

When the Inward Truck carries a real `purchase_order`/`purchase_order_item` (Phase
4), the posted Purchase Receipt links back to that PO line too — so ERPNext's own
`received_qty` tracking on the PO Item updates automatically, and the receipt rate
prefers the PO line's negotiated rate over the Phase 2 price proposal's purchaseCost.
A truck with no PO (poId is optional in the frontend too) still posts a standalone
receipt against just the supplier.

`get_allocation_qr_codes` (Phase 13) generates one QR image per bay split, encoding
the exact "PI-ITEM|<item>|<batch>|<bayCode>" string `resolve_scan` below already
parses — so a scan straight off the printed slip resolves the lot with no new scan
format to support. Codes are generated on demand from `qrcode` (a real ERPNext
dependency already, used for its own UPI/e-invoice QR features — not something
this app needs to add) rather than stored: the payload is fully determined by the
allocation's own fields, so there's nothing here worth persisting.
"""

import base64
from html import escape as _esc
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import today

from dms_erp.pagination import clamp
from dms_erp.pricing.api import get_price_record
from dms_erp.warehouse.bay_api import BAY_WRITE_ROLES
from dms_erp.warehouse.utils import default_company, ensure_batch, get_bay, list_stock_lots, validate_allocation


def _assert_can_allocate():
	if not set(frappe.get_roles(frappe.session.user)) & BAY_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can allocate bays."), frappe.PermissionError)


def _batch_weight_per_box_kg(batch_no: str | None) -> float | None:
	if not batch_no:
		return None
	return frappe.db.get_value("Batch", batch_no, "custom_batch_weight_kg")


def _serialize(doc) -> dict:
	weight_per_box_kg = _batch_weight_per_box_kg(doc.batch_no)
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
		"weightPerBoxKg": weight_per_box_kg,
		"totalWeightKg": (weight_per_box_kg or 0) * doc.total_qty if weight_per_box_kg is not None else None,
		"allocations": [
			{"bayId": row.bay, "bayCode": frappe.db.get_value("Warehouse", row.bay, "custom_bay_code"), "qty": row.qty, "confirmed": bool(row.confirmed)}
			for row in doc.lines
		],
		"createdAt": doc.creation,
	}


@frappe.whitelist(methods=["GET"])
def list_allocations(
	inward_truck: str | None = None,
	status: str | None = None,
	item: str | None = None,
	search: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	limit, offset = clamp(limit, offset)
	filters = {}
	if inward_truck:
		filters["inward_truck"] = inward_truck
	if status:
		filters["status"] = status
	if item:
		filters["item"] = item
	if search:
		filters["name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Bay Allocation", filters=filters)
	names = frappe.get_all(
		"Bay Allocation", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Bay Allocation", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_allocation(allocation: str):
	return _serialize(frappe.get_doc("Bay Allocation", allocation))


@frappe.whitelist(methods=["POST"])
def create_allocation(
	item: str,
	batch_no: str,
	total_qty: float,
	lines: list[dict],
	inward_truck: str | None = None,
	supplier: str | None = None,
	weight_per_box_kg: float | None = None,
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

	ensure_batch(item, batch_no, weight_per_box_kg=weight_per_box_kg)

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

	# Prefer the PO line's own negotiated rate (now that Phase 4 has real POs) over
	# the Phase 2 price proposal's purchaseCost, which was only ever a stand-in.
	po_item_row = frappe.db.get_value("Purchase Order Item", truck.purchase_order_item, "rate") if truck and truck.purchase_order_item else None
	if po_item_row is not None:
		rate = po_item_row
	else:
		price_record = get_price_record(item)
		rate = (price_record or {}).get("purchaseCost") or 0

	pr = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"supplier": supplier,
			"company": default_company(),
			"posting_date": today(),
			"items": [
				{
					"item_code": item,
					"qty": l["qty"],
					"warehouse": l["bay_name"],
					"batch_no": batch_no,
					"rate": rate,
					**(
						{"purchase_order": truck.purchase_order, "purchase_order_item": truck.purchase_order_item}
						if truck and truck.purchase_order and truck.purchase_order_item
						else {}
					),
				}
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


def _qr_data_uri(payload: str) -> str:
	import qrcode

	img = qrcode.make(payload)
	buf = BytesIO()
	img.save(buf, format="PNG")
	return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@frappe.whitelist(methods=["GET"])
def get_allocation_qr_codes(allocation: str):
	"""One QR per bay split on this allocation slip — each encodes the same
	"PI-ITEM|<item>|<batch>|<bayCode>" format resolve_scan already parses."""
	doc = frappe.get_doc("Bay Allocation", allocation)
	out = []
	for row in doc.lines:
		bay_code = frappe.get_cached_value("Warehouse", row.bay, "custom_bay_code")
		payload = f"PI-ITEM|{doc.item}|{doc.batch_no}|{bay_code}"
		out.append(
			{
				"bayId": row.bay,
				"bayCode": bay_code,
				"qty": row.qty,
				"payload": payload,
				"qrCode": _qr_data_uri(payload),
			}
		)
	return out


@frappe.whitelist(methods=["GET"])
def get_box_sticker_data(allocation: str):
	"""BRD C.6.4 box/inward sticker layout — one row per bay split, everything the
	sticker Print Format needs (company item code, product name, size/finish/series,
	batch + date of manufacture, bay, boxes, weight, PR + supplier references), on
	top of the same QR payload get_allocation_qr_codes already generates. This is the
	data layer only: the sticker's actual visual layout is a Frappe Print Format,
	whose label size/printer hardware the BRD itself leaves for setup-time
	confirmation with Pacific, not something this endpoint can decide.

	The dealer sample sticker (BRD C.10) is a separate layout gated on the Sample &
	Display Management module, which doesn't exist yet — not built here."""
	doc = frappe.get_doc("Bay Allocation", allocation)
	item_doc = frappe.get_cached_doc("Item", doc.item)
	batch = frappe.db.get_value("Batch", doc.batch_no, ["manufacturing_date", "custom_batch_weight_kg"], as_dict=True) or {}
	weight_per_box_kg = batch.get("custom_batch_weight_kg")
	if weight_per_box_kg is None:
		weight_per_box_kg = item_doc.custom_weight_per_box_kg

	po_supplier = None
	if doc.purchase_order:
		po_supplier = frappe.db.get_value("Purchase Order", doc.purchase_order, "supplier")

	rows = []
	for qr in get_allocation_qr_codes(allocation):
		rows.append(
			{
				**qr,
				"itemCode": doc.item,
				"itemName": item_doc.item_name,
				"size": item_doc.custom_size,
				"finish": item_doc.custom_finish,
				"series": item_doc.custom_series,
				"batchNumber": doc.batch_no,
				"dateOfManufacture": batch.get("manufacturing_date"),
				"piecesPerBox": item_doc.custom_pieces_per_box,
				"weightPerBoxKg": weight_per_box_kg,
				"purchaseReceipt": doc.purchase_receipt,
				"purchaseOrder": doc.purchase_order,
				"supplier": po_supplier,
			}
		)
	return rows


@frappe.whitelist(methods=["POST"])
def print_box_stickers(allocation: str, copies_per_box: int = 1):
	"""BRD C.6.4 "per-box print run" — one sticker entry per physical box (not per
	bay split), so a bay split of 60 boxes yields 60 (× copies_per_box) sticker
	entries the Print Format renders one-per-page from. `copies_per_box` covers a
	reprint (e.g. a damaged label) without re-running the whole allocation."""
	_assert_can_allocate()

	if copies_per_box < 1:
		frappe.throw(_("copies_per_box must be at least 1."), frappe.ValidationError)

	rows = get_box_sticker_data(allocation)
	stickers = []
	for row in rows:
		box_count = int(row["qty"]) * int(copies_per_box)
		stickers.extend([row] * box_count)
	return {"allocation": allocation, "count": len(stickers), "stickers": stickers}


_BOX_STICKER_CSS = """
	@page { size: 4in 2in; margin: 0.08in; }
	* { box-sizing: border-box; }
	body { margin: 0; font-family: -apple-system, Helvetica, Arial, sans-serif; }
	.sticker { width: 4in; height: 2in; padding: 0.1in 0.14in; display: flex; gap: 0.12in; page-break-after: always; }
	.sticker:last-child { page-break-after: auto; }
	.sticker .qr { width: 1in; height: 1in; flex-shrink: 0; }
	.sticker .info { font-size: 9pt; line-height: 1.35; overflow: hidden; }
	.sticker .info .code { font-size: 12pt; font-weight: 700; }
	.sticker .info .name { font-size: 10pt; font-weight: 600; }
	.sticker .info .row .k { color: #555; }
"""


@frappe.whitelist(methods=["GET", "POST"])
def render_box_stickers_html(allocation: str, copies_per_box: int = 1) -> str:
	"""BRD C.6.4's actual sticker layout — print_box_stickers' data (one entry per
	physical box) rendered as a real printable HTML sheet, sized to a standard 4x2in
	label via @page CSS. Returned as a plain HTML string in the ordinary JSON envelope
	(not a Desk Print Format, which this API-only app deliberately avoids — see the
	"no request from this app should ever redirect into /app" note in hooks.py) so the
	frontend can open it in a new tab/window and hand off to the browser's own
	Print / Save-as-PDF, which is also where the BRD's setup-time label size/printer
	confirmation with Pacific ultimately gets applied."""
	payload = print_box_stickers(allocation, copies_per_box)
	cards = []
	for s in payload["stickers"]:
		weight = s.get("weightPerBoxKg")
		weight_label = f"{weight} kg" if weight is not None else "—"
		dom = s.get("dateOfManufacture")
		cards.append(
			f"""<div class="sticker">
	<img class="qr" src="{s['qrCode']}" alt="QR">
	<div class="info">
		<div class="code">{_esc(s['itemCode'])}</div>
		<div class="name">{_esc(s.get('itemName') or '')}</div>
		<div class="row"><span class="k">Size/Finish:</span> {_esc(s.get('size') or '—')} / {_esc(s.get('finish') or '—')}</div>
		<div class="row"><span class="k">Batch:</span> {_esc(s['batchNumber'] or '')}</div>
		<div class="row"><span class="k">DOM:</span> {_esc(str(dom) if dom else '—')}</div>
		<div class="row"><span class="k">Weight:</span> {weight_label}</div>
		<div class="row"><span class="k">Bay:</span> {_esc(s.get('bayCode') or '')}</div>
	</div>
</div>"""
		)
	return f"<!doctype html><html><head><meta charset='utf-8'><title>Box Stickers</title><style>{_BOX_STICKER_CSS}</style></head><body>{''.join(cards)}</body></html>"


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
