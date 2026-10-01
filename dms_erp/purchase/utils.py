"""Shared purchase-domain helpers used by both reorder_api.py and
vendor_enquiry_api.py, to avoid the two drifting on MOQ resolution.
"""

import frappe

MOQ_KINDS = {"stock": "custom_moq", "production": "custom_production_moq"}


def resolve_moq(item_code: str, supplier: str | None, kind: str = "stock") -> int:
	"""BRD C.4.2: "MOQ may apply at company, vendor/manufacturer and item
	level, and production MOQ may differ from stock MOQ." Item -> Supplier ->
	Company, first level with a real (truthy) value wins; `kind` picks which
	of the two MOQ concepts to resolve, since a vendor/item/company can carry
	both independently."""
	if kind not in MOQ_KINDS:
		frappe.throw(f"Invalid MOQ kind: {kind}")
	fieldname = MOQ_KINDS[kind]

	item_moq = frappe.get_cached_value("Item", item_code, fieldname)
	if item_moq:
		return item_moq

	if supplier:
		supplier_moq = frappe.get_cached_value("Supplier", supplier, fieldname)
		if supplier_moq:
			return supplier_moq

	settings_field = "default_moq" if kind == "stock" else "default_production_moq"
	return frappe.db.get_single_value("DMS Purchase Settings", settings_field) or 0
