"""Bay master (BRD §10) — bays are ERPNext Warehouses (see warehouse/setup.py and
warehouse/utils.py for the mapping and serialization).

list_bays is paginated (`limit`/`offset`, via dms_erp.pagination.clamp) and
returns `{"items", "total", "limit", "offset"}`, not a bare list -- reports and
the warehouse dashboard need the whole result set, so they call list_all_bays
(unpaginated, internal-only) instead of the whitelisted endpoint.
"""

import frappe
from frappe import _

from dms_erp.pagination import clamp
from dms_erp.warehouse.utils import (
	DEFAULT_BACK_MARGIN_BOXES,
	DEFAULT_FRONT_MARGIN_BOXES,
	DEFAULT_SLOT_GAP_MM,
	DEFAULT_TOP_CLEARANCE_MM,
	MM_PER_FT,
	bay_size_label,
	compute_bay_capacity,
	default_company,
	get_bay,
	serialize_bay,
)

BAY_WRITE_ROLES = {"DMS Warehouse", "DMS Management", "System Manager"}

# camelCase patch key -> (Warehouse fieldname, human label). Order is the order the form shows them.
GEOMETRY_FIELDS = {
	"lengthMm": ("custom_length_mm", "Bay length"),
	"widthMm": ("custom_width_mm", "Bay width"),
	"heightMm": ("custom_height_mm", "Bay height"),
	"slotGapMm": ("custom_slot_gap_mm", "Gap between slots"),
	"backMarginBoxes": ("custom_back_margin_boxes", "Back margin"),
	"frontMarginBoxes": ("custom_front_margin_boxes", "Front margin"),
	"topClearanceMm": ("custom_top_clearance_mm", "Top clearance"),
}


def _geometry_values(
	length_mm, width_mm, height_mm, slot_gap_mm, back_margin_boxes, front_margin_boxes, top_clearance_mm
) -> dict:
	"""Validate the seven size inputs and turn them into the Warehouse fields to save --
	including the derived "40x6" label and the computed capacity, which are never typed in.

	Every one is required, zero included only where zero is meaningful (a gap, a margin or a
	clearance can be 0; a length, width or height cannot). A bay saved without them would have
	no capacity to compute, which is exactly the gap the old hand-entered number papered over."""
	given = {
		"lengthMm": length_mm,
		"widthMm": width_mm,
		"heightMm": height_mm,
		"slotGapMm": slot_gap_mm,
		"backMarginBoxes": back_margin_boxes,
		"frontMarginBoxes": front_margin_boxes,
		"topClearanceMm": top_clearance_mm,
	}
	missing = [GEOMETRY_FIELDS[key][1] for key, value in given.items() if value in (None, "")]
	if missing:
		frappe.throw(_("Required: {0}.").format(", ".join(missing)), frappe.ValidationError)

	try:
		length_mm, width_mm, height_mm, slot_gap_mm, top_clearance_mm = (
			float(v) for v in (length_mm, width_mm, height_mm, slot_gap_mm, top_clearance_mm)
		)
		back_margin_boxes, front_margin_boxes = int(back_margin_boxes), int(front_margin_boxes)
	except (TypeError, ValueError):
		frappe.throw(_("Bay sizes must be numbers (millimetres; margins in whole boxes)."), frappe.ValidationError)

	if min(length_mm, width_mm, height_mm) <= 0:
		frappe.throw(_("Bay length, width and height must be more than zero."), frappe.ValidationError)
	if min(slot_gap_mm, top_clearance_mm, back_margin_boxes, front_margin_boxes) < 0:
		frappe.throw(_("Gap, margins and top clearance cannot be negative."), frappe.ValidationError)
	if top_clearance_mm >= height_mm:
		frappe.throw(_("Top clearance must be less than the bay height."), frappe.ValidationError)

	capacity = compute_bay_capacity(length_mm, width_mm, slot_gap_mm, back_margin_boxes, front_margin_boxes)
	return {
		"custom_length_mm": length_mm,
		"custom_width_mm": width_mm,
		"custom_height_mm": height_mm,
		"custom_slot_gap_mm": slot_gap_mm,
		"custom_back_margin_boxes": back_margin_boxes,
		"custom_front_margin_boxes": front_margin_boxes,
		"custom_top_clearance_mm": top_clearance_mm,
		"custom_dimensions": bay_size_label(length_mm, width_mm),
		"custom_capacity_boxes": capacity["capacityBoxes"],
	}


def _assert_can_manage_bays():
	if not set(frappe.get_roles(frappe.session.user)) & BAY_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage bays."), frappe.PermissionError)


def _assert_valid_main_bay_link(linked_main_bay: str | None):
	"""BRD 1.9 -- a buffer bay's habitual main-bay destination must actually be a
	main bay, not any Warehouse (a group warehouse, or a damage/display bay)."""
	if not linked_main_bay:
		return
	bay_type = frappe.db.get_value("Warehouse", linked_main_bay, "custom_bay_type")
	if bay_type != "main":
		frappe.throw(_("{0} is not a main bay.").format(linked_main_bay), frappe.ValidationError)


@frappe.whitelist(methods=["GET"])
def list_bay_dimensions():
	"""Distinct `custom_dimensions` values already in use across real bays, for the
	Bay Master form's size picker -- BRD Part F.9 treats real bay sizes as a
	client-provided dependency, not a fixed list (see warehouse/setup.py's own
	custom_dimensions field, freed from a closed Select for exactly this reason),
	so the picker's options are whatever's actually been used so far, not a
	hardcoded set. Falls back to the original 4 example presets when nothing's
	been entered yet, so the picker isn't empty on a fresh site."""
	from dms_erp.warehouse.setup import BAY_DIMENSIONS

	values = frappe.get_all(
		"Warehouse",
		filters={"custom_bay_code": ["is", "set"], "custom_dimensions": ["is", "set"]},
		pluck="custom_dimensions",
		distinct=True,
	)
	return sorted(set(values)) or BAY_DIMENSIONS


@frappe.whitelist(methods=["GET"])
def list_bay_size_options():
	"""Dropdown options for each of the bay form's seven size fields: every value already
	saved on a real bay, plus a starting set so a fresh site's dropdowns aren't empty.

	This is how "save a value once, pick it on the next bay" works -- there is no separate
	list to maintain; typing a new value into the form and saving the bay is what adds it.
	Length/width/height start from the four example bay sizes in the BRD (in mm); gap, margins
	and clearance start from the values agreed with the client."""
	from dms_erp.warehouse.setup import BAY_DIMENSIONS

	seeds = {
		"lengthMm": {round(float(d.split("x")[0]) * MM_PER_FT, 1) for d in BAY_DIMENSIONS},
		"widthMm": {round(float(d.split("x")[1]) * MM_PER_FT, 1) for d in BAY_DIMENSIONS},
		"heightMm": set(),
		"slotGapMm": {DEFAULT_SLOT_GAP_MM},
		"backMarginBoxes": {DEFAULT_BACK_MARGIN_BOXES},
		"frontMarginBoxes": {DEFAULT_FRONT_MARGIN_BOXES},
		"topClearanceMm": {DEFAULT_TOP_CLEARANCE_MM},
	}
	options = {}
	for key, (fieldname, _label) in GEOMETRY_FIELDS.items():
		saved = frappe.get_all(
			"Warehouse",
			filters={"custom_bay_code": ["is", "set"], fieldname: ["is", "set"]},
			pluck=fieldname,
			distinct=True,
		)
		options[key] = sorted(seeds[key] | {v for v in saved if v is not None})
	return options


@frappe.whitelist(methods=["GET"])
def list_warehouse_groups(search: str | None = None):
	# The other half of "bays are ERPNext Warehouses" (see module docstring):
	# create_bay's parent_warehouse wants a group Warehouse's raw `name`, which
	# ERPNext autonames with a company-abbreviation suffix (e.g. "Pacific Main —
	# Morbi - PI") -- different from its clean warehouse_name, and nothing else
	# exposed that raw id. This is that lookup, kept minimal since a bay group
	# has nothing worth serializing beyond id/name (unlike a bay itself).
	#
	# search, no pagination: create_warehouse_group means this list can now grow
	# past the original two, but a physical-site directory stays small (dozens,
	# not the thousands a product/order list can reach) -- same shape as
	# dealer_api.list_dealers/supplier_api.list_suppliers, not the paginated
	# {items, total} list endpoints.
	filters = {"is_group": 1}
	if search:
		filters["warehouse_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all("Warehouse", filters=filters, fields=["name", "warehouse_name"], order_by="warehouse_name asc")
	return [{"id": r.name, "name": r.warehouse_name} for r in rows]


@frappe.whitelist(methods=["POST"])
def create_warehouse_group(name: str):
	# The two physical warehouses (warehouse/setup.py's PHYSICAL_WAREHOUSES) are
	# only the initial seed, not a hard limit -- a new physical site is created
	# through here, the same way a bay is created through create_bay, and its
	# `parent_warehouse` (none, since a group warehouse sits at the root) is why
	# it needs no such param itself.
	_assert_can_manage_bays()

	if frappe.db.exists("Warehouse", {"warehouse_name": name, "company": default_company()}):
		frappe.throw(_("A warehouse named {0} already exists.").format(name), frappe.DuplicateEntryError)

	group = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": name,
			"company": default_company(),
			"is_group": 1,
		}
	)
	group.insert(ignore_permissions=True)
	return {"id": group.name, "name": group.warehouse_name}


def _bay_filters(
	search: str | None, bay_type: str | None = None, status: str | None = None, parent_warehouse: str | None = None
) -> dict:
	filters = {"is_group": 0}
	# search's LIKE pattern already can't match an empty string, so it doubles as
	# the "has a real bay code" exclusion the no-search case needs explicitly.
	filters["custom_bay_code"] = ["like", f"%{search}%"] if search else ["!=", ""]
	if bay_type:
		filters["custom_bay_type"] = bay_type
	if status:
		filters["custom_bay_status"] = status
	if parent_warehouse:
		filters["parent_warehouse"] = parent_warehouse
	return filters


def list_all_bays(
	search: str | None = None, bay_type: str | None = None, status: str | None = None, parent_warehouse: str | None = None
) -> list[dict]:
	"""Unpaginated -- for internal callers (reports, dashboard) that need the full
	result set, not a page of it. list_bays (the whitelisted endpoint) is the
	paginated one."""
	filters = _bay_filters(search, bay_type, status, parent_warehouse)
	names = frappe.get_all("Warehouse", filters=filters, pluck="name")
	return [serialize_bay(frappe.get_doc("Warehouse", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_bays(
	search: str | None = None,
	bay_type: str | None = None,
	status: str | None = None,
	parent_warehouse: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	limit, offset = clamp(limit, offset)
	filters = _bay_filters(search, bay_type, status, parent_warehouse)
	total = frappe.db.count("Warehouse", filters=filters)
	names = frappe.get_all(
		"Warehouse", filters=filters, pluck="name", order_by="custom_bay_code asc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [serialize_bay(frappe.get_doc("Warehouse", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_bay_detail(code: str):
	return serialize_bay(get_bay(code))


@frappe.whitelist(methods=["POST"])
def create_bay(
	code: str,
	bay_type: str,
	parent_warehouse: str,
	zone: str,
	row: str,
	length_mm: float,
	width_mm: float,
	height_mm: float,
	slot_gap_mm: float,
	back_margin_boxes: int,
	front_margin_boxes: int,
	top_clearance_mm: float,
	suitable_categories: list[str] | None = None,
	status: str = "active",
	linked_main_bay: str | None = None,
):
	_assert_can_manage_bays()

	if frappe.db.exists("Warehouse", {"custom_bay_code": code}):
		frappe.throw(_("Bay {0} already exists.").format(code), frappe.DuplicateEntryError)
	_assert_valid_main_bay_link(linked_main_bay)
	geometry = _geometry_values(
		length_mm, width_mm, height_mm, slot_gap_mm, back_margin_boxes, front_margin_boxes, top_clearance_mm
	)

	bay = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": f"{bay_type.title()} Bay {code}",
			"company": default_company(),
			"parent_warehouse": parent_warehouse,
			"is_group": 0,
			"custom_bay_code": code,
			"custom_bay_type": bay_type,
			**geometry,
			"custom_zone": zone,
			"custom_row": row,
			"custom_bay_status": "blocked" if bay_type == "blocked" else status,
			"custom_suitable_categories": ", ".join(suitable_categories or []),
			"custom_linked_main_bay": linked_main_bay,
		}
	)
	bay.insert(ignore_permissions=True)
	return serialize_bay(bay)


@frappe.whitelist(methods=["POST"])
def create_bay_grid(
	prefix: str,
	count: int,
	start_at: int,
	bay_type: str,
	parent_warehouse: str,
	zone: str,
	length_mm: float,
	width_mm: float,
	height_mm: float,
	slot_gap_mm: float,
	back_margin_boxes: int,
	front_margin_boxes: int,
	top_clearance_mm: float,
	categories: list[str] | None = None,
):
	_assert_can_manage_bays()

	created = 0
	for i in range(int(count)):
		n = str(int(start_at) + i).zfill(2)
		code = f"{prefix}-{n}"
		if frappe.db.exists("Warehouse", {"custom_bay_code": code}):
			continue
		create_bay(
			code=code,
			bay_type=bay_type,
			parent_warehouse=parent_warehouse,
			zone=zone,
			row=f"Row-{i + 1}",
			length_mm=length_mm,
			width_mm=width_mm,
			height_mm=height_mm,
			slot_gap_mm=slot_gap_mm,
			back_margin_boxes=back_margin_boxes,
			front_margin_boxes=front_margin_boxes,
			top_clearance_mm=top_clearance_mm,
			suitable_categories=categories,
		)
		created += 1
	return {"created": created}


@frappe.whitelist(methods=["POST", "PUT"])
def update_bay(code: str, patch: dict):
	_assert_can_manage_bays()

	# `dimensions` and `capacityBoxes` are deliberately not patchable any more: both are
	# derived from the size fields (see _geometry_values), so accepting them would let a
	# client store a label or capacity that disagrees with the real bay size.
	field_map = {
		"name": "warehouse_name",
		"type": "custom_bay_type",
		"status": "custom_bay_status",
		"zone": "custom_zone",
		"row": "custom_row",
		"warehouse": "parent_warehouse",
		"linkedMainBay": "custom_linked_main_bay",
	}

	if "linkedMainBay" in patch:
		_assert_valid_main_bay_link(patch["linkedMainBay"])

	bay = get_bay(code)
	if any(key in patch for key in GEOMETRY_FIELDS):
		# Re-validate and recompute from the full set (what's stored, overlaid by what's
		# patched) so changing one size can't leave capacity worked out from the old ones.
		merged = {
			key: patch[key] if key in patch else bay.get(fieldname) for key, (fieldname, _label) in GEOMETRY_FIELDS.items()
		}
		for fieldname, value in _geometry_values(*merged.values()).items():
			bay.set(fieldname, value)
	for key, value in patch.items():
		if key == "suitableCategories":
			bay.custom_suitable_categories = ", ".join(value or [])
			continue
		fieldname = field_map.get(key)
		if fieldname:
			bay.set(fieldname, value)
	bay.save(ignore_permissions=True)
	return serialize_bay(bay)


@frappe.whitelist(methods=["POST", "DELETE"])
def delete_bay(code: str):
	_assert_can_manage_bays()
	bay = get_bay(code)
	# frappe.delete_doc already refuses a Warehouse with existing stock/ledger
	# entries (LinkExistsError) — no need to duplicate that check here.
	frappe.delete_doc("Warehouse", bay.name, ignore_permissions=True)
	return {"success": True}
