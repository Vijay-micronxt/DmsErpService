"""Header ⌘K search (BRD §1 "Global search bar (cmd+k style)") -- server-side, so it
covers every record rather than only whatever the frontend happened to have loaded,
and so it reaches documents with no screen of their own in the app yet (Sales/Purchase
Invoice, Payment Entry, Journal Entry, Delivery Note), which the frontend opens in the
ERPNext desk instead.

Two kinds of source:
  "app"  -- doctypes the app has its own screen for. Gated by the same roles as that
            screen's API (any staff role unless the source names its own).
  "desk" -- doctypes only the ERPNext desk can open. Gated by real doctype read
            permission, since that's exactly what decides whether the desk link works.

Search fields that don't exist on a site (the tally_* fields only exist where the Tally
migration ran) are dropped per site rather than erroring.
"""

import frappe
from frappe import _

from dms_erp.finance.payment_api import PAYMENT_READ_ROLES
from dms_erp.sales.invoice_api import INVOICE_READ_ROLES

STAFF_ROLES = {"DMS Sales", "DMS Purchase", "DMS Warehouse", "DMS Management", "System Manager"}
MIN_QUERY_LENGTH = 2
RESULTS_PER_DOCTYPE = 5

SOURCES = [
	{"doctype": "Customer", "label": "Dealers", "kind": "app", "title": "customer_name", "subtitle": ["customer_group", "custom_phone"],
	 "search": ["name", "customer_name", "custom_phone", "custom_gstin", "gstin"]},
	{"doctype": "Supplier", "label": "Suppliers", "kind": "app", "title": "supplier_name", "subtitle": ["supplier_group"],
	 "search": ["name", "supplier_name", "gstin"]},
	{"doctype": "Item", "label": "Items", "kind": "app", "title": "item_name", "subtitle": ["item_group", "custom_size"],
	 "search": ["name", "item_name"]},
	{"doctype": "Inquiry", "label": "Inquiries", "kind": "app", "title": "name", "subtitle": ["dealer", "item", "status"],
	 "search": ["name", "dealer", "item", "contact_name", "contact_phone", "customer_po"]},
	{"doctype": "Quotation", "label": "Quotations", "kind": "app", "title": "name", "subtitle": ["customer_name", "status"],
	 "search": ["name", "party_name", "customer_name", "tally_voucher_no"]},
	{"doctype": "Sales Order", "label": "Orders", "kind": "app", "title": "name", "subtitle": ["customer_name", "status"],
	 "search": ["name", "customer", "customer_name", "po_no"]},
	{"doctype": "Purchase Order", "label": "Purchase Orders", "kind": "app", "title": "name", "subtitle": ["supplier_name", "status"],
	 "search": ["name", "supplier", "supplier_name", "tally_voucher_no"]},
	{"doctype": "Pickup Run", "label": "Pickup Runs", "kind": "app", "title": "name", "subtitle": ["supplier", "status"],
	 "search": ["name", "supplier", "vehicle_number"]},
	{"doctype": "Inward Truck", "label": "Inward Trucks", "kind": "app", "title": "vehicle_number", "subtitle": ["supplier", "lr_number", "status"],
	 "search": ["lr_number", "vehicle_number", "supplier", "po_reference"]},
	{"doctype": "Transporter", "label": "Transporters", "kind": "app", "title": "name", "subtitle": ["contact"],
	 "search": ["name", "contact"]},
	{"doctype": "Sample Request", "label": "Sample Requests", "kind": "app", "title": "name", "subtitle": ["dealer", "item", "approval_status"],
	 "search": ["name", "dealer", "item"]},
	{"doctype": "Insurance Claim", "label": "Claims", "kind": "app", "title": "name", "subtitle": ["supplier", "status"],
	 "search": ["name", "supplier", "insurer"]},
	{"doctype": "Sales Invoice", "label": "Sales Invoices", "kind": "app", "roles": INVOICE_READ_ROLES, "title": "name",
	 "subtitle": ["customer_name", "posting_date"],
	 "search": ["name", "customer", "customer_name", "tally_voucher_no", "tally_lr_no", "tally_vehicle_no", "tally_eway_bill_no"]},
	{"doctype": "Purchase Invoice", "label": "Purchase Invoices", "kind": "desk", "title": "name", "subtitle": ["supplier_name", "posting_date"],
	 "search": ["name", "supplier", "supplier_name", "bill_no", "tally_voucher_no", "tally_lr_no", "tally_vehicle_no"]},
	{"doctype": "Payment Entry", "label": "Payments", "kind": "app", "roles": PAYMENT_READ_ROLES, "title": "name",
	 "subtitle": ["party_name", "posting_date"],
	 "search": ["name", "party", "party_name", "reference_no", "tally_voucher_no"]},
	{"doctype": "Journal Entry", "label": "Journal Entries", "kind": "desk", "title": "name", "subtitle": ["title", "posting_date"],
	 "search": ["name", "title", "cheque_no", "tally_voucher_no"]},
	{"doctype": "Delivery Note", "label": "Delivery Notes", "kind": "desk", "title": "name", "subtitle": ["customer_name", "posting_date"],
	 "search": ["name", "customer", "customer_name", "tally_voucher_no", "tally_lr_no", "tally_vehicle_no"]},
]


def _existing(meta, fields: list[str]) -> list[str]:
	return [f for f in fields if f == "name" or meta.has_field(f)]


def _visible_sources() -> list[dict]:
	roles = set(frappe.get_roles())

	def allowed(s: dict) -> bool:
		if s["kind"] == "desk":
			return frappe.has_permission(s["doctype"], "read")
		return bool(roles & s.get("roles", STAFF_ROLES))

	return [s for s in SOURCES if frappe.db.exists("DocType", s["doctype"]) and allowed(s)]


def _search_source(source: dict, query: str) -> list[dict]:
	meta = frappe.get_meta(source["doctype"])
	search_fields = _existing(meta, source["search"])
	subtitle_fields = _existing(meta, source["subtitle"])
	title_field = source["title"] if source["title"] == "name" or meta.has_field(source["title"]) else "name"
	pattern = f"%{query}%"
	rows = frappe.get_all(
		source["doctype"],
		or_filters={f: ["like", pattern] for f in search_fields},
		fields=list({"name", title_field, *subtitle_fields}),
		order_by="modified desc",
		limit_page_length=RESULTS_PER_DOCTYPE,
	)
	return [
		{
			"doctype": source["doctype"],
			"name": row.name,
			"title": str(row.get(title_field) or row.name),
			"subtitle": " · ".join(str(row.get(f)) for f in subtitle_fields if row.get(f)),
		}
		for row in rows
	]


@frappe.whitelist(methods=["GET"])
def global_search(query: str):
	if not set(frappe.get_roles()) & STAFF_ROLES and not any(
		frappe.has_permission(s["doctype"], "read") for s in SOURCES if s["kind"] == "desk"
	):
		frappe.throw(_("Search is only available to staff."), frappe.PermissionError)

	query = (query or "").strip()
	sources = _visible_sources()
	lowered = query.lower()
	doctypes = [
		{"doctype": s["doctype"], "label": s["label"], "kind": s["kind"]}
		for s in sources
		if lowered and (lowered in s["label"].lower() or lowered in s["doctype"].lower())
	]
	if len(query) < MIN_QUERY_LENGTH:
		return {"doctypes": doctypes, "groups": []}

	groups = []
	for source in sources:
		results = _search_source(source, query)
		if results:
			groups.append({"doctype": source["doctype"], "label": source["label"], "kind": source["kind"], "results": results})
	return {"doctypes": doctypes, "groups": groups}
