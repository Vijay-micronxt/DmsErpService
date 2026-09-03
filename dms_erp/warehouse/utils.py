"""Bay = ERPNext Warehouse. Everything here reads/writes through Warehouse, Bin,
Stock Ledger Entry and Batch — there is no separate "lot" table; BayLot from the
frontend is a live aggregate over Stock Ledger Entry (grouped by item+warehouse+
batch), not a doctype of its own.
"""

import frappe

CAPACITY_FOR = {"36x8": 900, "36x6": 700, "32x8": 800, "32x6": 600}
DAMAGE_BAY_TYPES = {"damage", "insurance_claim"}


def default_company() -> str:
	company = frappe.defaults.get_global_default("company")
	if not company:
		frappe.throw("No default Company is configured for this site yet.")
	return company


def get_bay(bay_code: str) -> "frappe.model.document.Document":
	name = frappe.db.get_value("Warehouse", {"custom_bay_code": bay_code}, "name")
	if not name:
		frappe.throw(f"No bay found for code {bay_code!r}", frappe.DoesNotExistError)
	return frappe.get_doc("Warehouse", name)


def suitable_categories(bay) -> list[str]:
	raw = bay.custom_suitable_categories or ""
	return [c.strip() for c in raw.split(",") if c.strip()]


def available_for_item(item_code: str) -> float:
	"""Free-to-pick qty for an item across every non-blocked bay — used by the Phase 5
	picking auto-allocate (mirrors the frontend's availableForProduct)."""
	blocked = set(
		frappe.get_all(
			"Warehouse",
			or_filters={"custom_bay_type": "blocked", "custom_bay_status": "blocked"},
			pluck="name",
		)
	)
	return sum(l["boxes"] for l in list_stock_lots(item=item_code) if l["bayId"] not in blocked)


def total_stock_for_item(item_code: str) -> float:
	"""Total on-hand qty for an item across every bay — used by the Phase 4 reorder
	engine as the real `currentStock` signal (Phase 2 could only stub this at 0)."""
	total = frappe.db.sql(
		"select sum(actual_qty) from `tabBin` where item_code=%s", (item_code,)
	)[0][0]
	return float(total or 0)


def bay_used_boxes(bay_name: str) -> float:
	total = frappe.db.sql(
		"select sum(actual_qty) from `tabBin` where warehouse=%s", (bay_name,)
	)[0][0]
	return float(total or 0)


def bay_occupancy(bay) -> dict:
	used = bay_used_boxes(bay.name)
	capacity = bay.custom_capacity_boxes or 0
	pct = round((used / capacity) * 100) if capacity else 0
	return {"current": used, "pct": pct, "free": max(0, capacity - used)}


def serialize_bay(bay) -> dict:
	occ = bay_occupancy(bay)
	# parent_warehouse is the company-abbreviation-suffixed doc name (ERPNext's own
	# Warehouse autoname behavior) — resolve it back to the clean display name.
	parent_name = frappe.get_cached_value("Warehouse", bay.parent_warehouse, "warehouse_name") if bay.parent_warehouse else None
	return {
		"id": bay.name,
		"code": bay.custom_bay_code,
		"name": bay.warehouse_name,
		"warehouse": parent_name,
		"type": bay.custom_bay_type,
		"dimensions": bay.custom_dimensions,
		"capacityBoxes": bay.custom_capacity_boxes,
		"suitableCategories": suitable_categories(bay),
		"status": bay.custom_bay_status,
		"zone": bay.custom_zone,
		"row": bay.custom_row,
		"occupiedBoxes": occ["current"],
		"occupancyPct": occ["pct"],
		"freeBoxes": occ["free"],
	}


def claim_ref_for_lot(bay_name: str, item_code: str, batch_no: str) -> str | None:
	"""Trace a lot back through Stock Ledger Entry to the Stock Entry that moved it
	into this bay, and return its `custom_claim_ref` (Phase 6) if one was filed.
	Shared by `list_stock_lots` (per-lot `claimRef`) and the Phase 8 dashboard's
	"damage awaiting claim" count, so both use the same trace.

	Same v15 Serial and Batch Bundle wrinkle as `list_stock_lots` (see its
	docstring) — `batch_no` lives on the bundle's Serial and Batch Entry rows,
	not on Stock Ledger Entry directly, once a site is on that batch-tracking
	model, so this needs the same left join rather than filtering `batch_no`
	on Stock Ledger Entry directly."""
	rows = frappe.db.sql(
		"""
		select distinct sle.voucher_no as voucher_no
		from `tabStock Ledger Entry` sle
		left join `tabSerial and Batch Entry` sbe on sbe.parent = sle.serial_and_batch_bundle
		where sle.warehouse = %(warehouse)s
			and sle.item_code = %(item_code)s
			and coalesce(sbe.batch_no, sle.batch_no) = %(batch_no)s
			and sle.voucher_type = 'Stock Entry'
			and coalesce(sbe.qty * if(sbe.is_outward, -1, 1), sle.actual_qty) > 0
		""",
		{"warehouse": bay_name, "item_code": item_code, "batch_no": batch_no},
		as_dict=True,
	)
	for row in rows:
		claim_ref = frappe.db.get_value("Stock Entry", row.voucher_no, "custom_claim_ref")
		if claim_ref:
			return claim_ref
	return None


def list_stock_lots(bay: str | None = None, item: str | None = None) -> list[dict]:
	"""Live aggregate of on-hand qty by item+warehouse+batch, sourced from Stock
	Ledger Entry (Bin has no batch dimension). Zero/negative-cleared batches are
	dropped, matching how the frontend's `lots` only ever lists what's physically
	present.

	ERPNext v15 moved batch tracking off the legacy `Stock Ledger Entry.batch_no`
	column and onto `Serial and Batch Bundle` (child table `Serial and Batch
	Entry`, one row per batch in the bundle, linked via `sle.serial_and_batch_bundle`).
	`batch_no` is left blank on every such entry now, so filtering on it directly
	silently returned nothing. This left-joins the bundle's per-batch rows and
	falls back to the legacy column for any entry that still uses it directly —
	`Serial and Batch Entry.qty` is always positive with a separate `is_outward`
	flag, unlike `sle.actual_qty` which is signed, so the outward case is negated
	to match."""
	conditions = ["sle.is_cancelled = 0"]
	values: dict = {}
	if bay:
		conditions.append("sle.warehouse = %(warehouse)s")
		values["warehouse"] = bay
	if item:
		conditions.append("sle.item_code = %(item)s")
		values["item"] = item

	rows = frappe.db.sql(
		f"""
		select
			sle.warehouse as bay,
			sle.item_code as item_code,
			coalesce(sbe.batch_no, sle.batch_no) as batch_no,
			sum(coalesce(sbe.qty * if(sbe.is_outward, -1, 1), sle.actual_qty)) as boxes,
			min(sle.posting_date) as stored_at
		from `tabStock Ledger Entry` sle
		left join `tabSerial and Batch Entry` sbe on sbe.parent = sle.serial_and_batch_bundle
		where {' and '.join(conditions)}
			and (sbe.batch_no is not null or (sle.batch_no is not null and sle.batch_no != ''))
		group by sle.warehouse, sle.item_code, coalesce(sbe.batch_no, sle.batch_no)
		having sum(coalesce(sbe.qty * if(sbe.is_outward, -1, 1), sle.actual_qty)) > 0
		""",
		values,
		as_dict=True,
	)

	out = []
	for row in rows:
		item_doc = frappe.get_cached_doc("Item", row.item_code)
		bay_type = frappe.get_cached_value("Warehouse", row.bay, "custom_bay_type")
		is_damage_bay = bay_type in DAMAGE_BAY_TYPES
		out.append(
			{
				# No dedicated "lot" doctype exists (see module docstring) — this is a
				# stable synthetic id over the group-by key, for frontend list-item keys only.
				"id": f"{row.bay}::{row.item_code}::{row.batch_no}",
				"bayId": row.bay,
				"itemCode": row.item_code,
				"productId": row.item_code,
				"itemName": item_doc.item_name,
				"category": item_doc.item_group,
				"batchNumber": row.batch_no,
				"boxes": row.boxes,
				"storedAt": str(row.stored_at),
				"damageType": bay_type if is_damage_bay else None,
				"claimRef": claim_ref_for_lot(row.bay, row.item_code, row.batch_no) if is_damage_bay else None,
			}
		)
	return out


def ensure_batch(item_code: str, batch_no: str) -> str:
	if frappe.db.exists("Batch", batch_no):
		return batch_no
	frappe.get_doc({"doctype": "Batch", "batch_id": batch_no, "item": item_code}).insert(ignore_permissions=True)
	return batch_no


def suggest_bays(category: str, qty: float, kind: str = "normal") -> dict:
	wanted_types = ["damage"] if kind == "damage" else ["insurance_claim"] if kind == "claim" else ["main"]

	def score(bay_row, free):
		s = 40
		if category in bay_row["suitableCategories"]:
			s += 45
		if free >= qty:
			s += 15
		existing = [l for l in list_stock_lots(bay=bay_row["id"])]
		if existing and all(l["category"] == category for l in existing):
			s += 10
		if any(l["category"] != category for l in existing):
			s -= 20
		return max(5, min(99, s))

	def build(types):
		bays = frappe.get_all(
			"Warehouse",
			filters={"custom_bay_type": ["in", types], "custom_bay_status": "active", "is_group": 0},
			pluck="name",
		)
		suggestions = []
		for name in bays:
			bay = frappe.get_doc("Warehouse", name)
			row = serialize_bay(bay)
			free = row["freeBoxes"]
			if free <= 0:
				continue
			suggestions.append(
				{
					"bay": row,
					"free": free,
					"pct": row["occupancyPct"],
					"score": score(row, free),
					"reason": "Full quantity fits" if free >= qty else f"Partial fit — {free} boxes free",
				}
			)
		suggestions.sort(key=lambda s: (-s["score"], -s["free"]))
		return suggestions

	return {"main": build(wanted_types)[:4], "buffer": build(["buffer"])[:3]}


def validate_allocation(bay_code: str, qty: float, category: str) -> list[dict]:
	bay = get_bay(bay_code)
	row = serialize_bay(bay)
	issues = []

	if bay.custom_bay_type == "blocked" or bay.custom_bay_status == "blocked":
		issues.append({"level": "error", "message": f"{row['code']} is blocked — cannot store material."})
	if qty > row["freeBoxes"]:
		issues.append({"level": "error", "message": f"Exceeds capacity of {row['code']} — only {row['freeBoxes']} boxes free."})
	cats = suitable_categories(bay)
	if cats and category not in cats:
		issues.append({"level": "warning", "message": f"{row['code']} is designated for {', '.join(cats)}. Incoming item is {category}."})

	mixed = [l for l in list_stock_lots(bay=bay.name) if l["category"] != category]
	if mixed:
		issues.append({"level": "warning", "message": f"{row['code']} already holds {mixed[0]['category']}. Mixing categories in one bay."})

	return issues
