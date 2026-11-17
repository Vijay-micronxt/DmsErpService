"""Printable Quotation, Sales Order and Purchase Order (client finding 8.3: "we have the
option of print but the format is blank").

Same approach as catalog.dealer_catalog_api.dealer_catalog_export and the sticker sheets:
plain, self-contained HTML in the ordinary JSON envelope, which the staff app opens in a
new window for the browser's own Print / Save as PDF. Not a Desk Print Format -- this app
never sends staff into /app, and the Desk formats were the blank ones.

Every value is read from the submitted ERPNext document itself (rates, discounts, tax rows,
totals), never recomputed here, so the printout always matches what was saved. Company
address/GSTIN appear only when they're set on the Company and its Address; nothing is
invented when they aren't.
"""

from html import escape

import frappe
from frappe import _
from frappe.utils import flt, formatdate, money_in_words

from dms_erp.catalog.utils import item_weight_per_box_kg
from dms_erp.warehouse.utils import default_company

PRINT_READ_ROLES = {"DMS Sales", "DMS Purchase", "DMS Warehouse", "DMS Management", "DMS Finance", "System Manager"}

_CSS = """
	* { box-sizing: border-box; }
	body { margin: 0; padding: 28px; font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1b2024; font-size: 12px; }
	.head { display: flex; justify-content: space-between; gap: 24px; border-bottom: 2px solid #1b2024; padding-bottom: 12px; }
	.co h1 { font-size: 18px; margin: 0 0 4px; }
	.co p, .doc p { margin: 1px 0; color: #444b50; }
	.doc { text-align: right; }
	.doc h2 { font-size: 16px; margin: 0 0 6px; letter-spacing: .04em; text-transform: uppercase; }
	.parties { display: flex; gap: 24px; margin: 16px 0; }
	.parties div { flex: 1; }
	.label { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: #6b7378; margin-bottom: 3px; }
	table { width: 100%; border-collapse: collapse; }
	th, td { border-bottom: 1px solid #dce0df; padding: 6px 8px; text-align: left; vertical-align: top; }
	th { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: #545c61; border-bottom: 1px solid #1b2024; }
	td.n, th.n { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
	.totals { margin-left: auto; width: 320px; margin-top: 12px; }
	.totals td { border: 0; padding: 3px 8px; }
	.totals tr.grand td { border-top: 2px solid #1b2024; font-weight: 700; font-size: 13px; padding-top: 6px; }
	.words { margin-top: 8px; color: #444b50; }
	.notes { margin-top: 16px; color: #444b50; }
	.sign { margin-top: 48px; display: flex; justify-content: space-between; gap: 24px; }
	.sign > div:only-child { margin-left: auto; }
	.sign div { border-top: 1px solid #1b2024; padding-top: 4px; width: 220px; text-align: center; color: #444b50; }
	@media print { body { padding: 0; } }
"""


def _assert_can_print():
	if not set(frappe.get_roles(frappe.session.user)) & PRINT_READ_ROLES:
		frappe.throw(_("Not permitted to print this document."), frappe.PermissionError)


def _e(value) -> str:
	return escape(str(value)) if value not in (None, "") else ""


def _money(value, currency: str = "INR") -> str:
	return frappe.utils.fmt_money(flt(value), currency=currency)


def _company_block() -> tuple[str, str]:
	"""(company name, the HTML lines under it). Address and GSTIN only when set."""
	company = default_company()
	doc = frappe.get_cached_doc("Company", company)
	lines = []
	address_name = frappe.db.get_value(
		"Dynamic Link", {"link_doctype": "Company", "link_name": company, "parenttype": "Address"}, "parent"
	)
	gstin = doc.get("gstin")
	if address_name:
		a = frappe.get_doc("Address", address_name)
		parts = [a.address_line1, a.address_line2, a.city, a.state, a.pincode]
		lines.append(", ".join(p for p in parts if p))
		gstin = gstin or a.get("gstin")
	if gstin:
		lines.append(f"GSTIN: {gstin}")
	if doc.get("phone_no"):
		lines.append(f"Phone: {doc.phone_no}")
	if doc.get("email"):
		lines.append(doc.email)
	return doc.company_name or company, "".join(f"<p>{_e(line)}</p>" for line in lines)


def _page(title: str, doc_label: str, doc_lines: list[tuple[str, str]], party_label: str, party_lines: list[str],
		  table: str, totals: str, notes: str = "", ship_label: str = "", ship_lines: list[str] | None = None,
		  receiver_sign: str = "") -> str:
	"""`ship_*` adds a second party block beside the first (Bill-to / Ship-to, BRD C.3.3);
	`receiver_sign` adds a "received by" line for documents the other side signs on delivery."""
	company_name, company_lines = _company_block()
	meta = "".join(f"<p>{_e(k)}: <b>{_e(v)}</b></p>" for k, v in doc_lines if v)
	party = "".join(f"<p>{_e(line)}</p>" for line in party_lines if line)
	ship = ""
	if ship_lines:
		ship = (
			f"<div><div class='label'>{_e(ship_label)}</div>"
			+ "".join(f"<p>{_e(line)}</p>" for line in ship_lines if line)
			+ "</div>"
		)
	receiver = f"<div>{_e(receiver_sign)}</div>" if receiver_sign else ""
	return (
		f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)}</title><style>{_CSS}</style></head><body>"
		f"<div class='head'><div class='co'><h1>{_e(company_name)}</h1>{company_lines}</div>"
		f"<div class='doc'><h2>{_e(doc_label)}</h2>{meta}</div></div>"
		f"<div class='parties'><div><div class='label'>{_e(party_label)}</div>{party}</div>{ship}</div>"
		f"{table}{totals}{notes}"
		f"<div class='sign'>{receiver}<div>For {_e(company_name)}</div></div>"
		"</body></html>"
	)


def _qty(row) -> str:
	return f"{flt(row.qty):g} {_e(row.uom or row.stock_uom or '')}".strip()


def _sales_lines_table(doc) -> tuple[str, float]:
	rows, total_weight = [], 0.0
	for i, row in enumerate(doc.items, start=1):
		weight = item_weight_per_box_kg(row.item_code)
		line_weight = flt(weight) * flt(row.qty) if weight is not None else None
		if line_weight:
			total_weight += line_weight
		discount = f"{flt(row.discount_percentage):g}%" if flt(row.discount_percentage) else ""
		rows.append(
			f"<tr><td class='n'>{i}</td><td><b>{_e(row.item_code)}</b><br>{_e(row.item_name)}</td>"
			f"<td class='n'>{_qty(row)}</td><td class='n'>{_money(row.price_list_rate or row.rate, doc.currency)}</td>"
			f"<td class='n'>{discount}</td><td class='n'>{_money(row.rate, doc.currency)}</td>"
			f"<td class='n'>{f'{line_weight:,.1f} kg' if line_weight else ''}</td>"
			f"<td class='n'>{_money(row.amount, doc.currency)}</td></tr>"
		)
	table = (
		"<table><thead><tr><th class='n'>#</th><th>Item</th><th class='n'>Qty</th><th class='n'>List rate</th>"
		"<th class='n'>Disc.</th><th class='n'>Rate</th><th class='n'>Weight</th><th class='n'>Amount</th></tr></thead>"
		f"<tbody>{''.join(rows)}</tbody></table>"
	)
	return table, total_weight


def _sales_totals(doc, freight: float = 0) -> str:
	lines = [("Net total", _money(doc.net_total, doc.currency))]
	for tax in doc.taxes or []:
		lines.append((tax.description or tax.account_head, _money(tax.tax_amount, doc.currency)))
	if freight:
		lines.append(("Freight", _money(freight, doc.currency)))
	grand = flt(doc.grand_total) + flt(freight)
	body = "".join(f"<tr><td>{_e(k)}</td><td class='n'>{v}</td></tr>" for k, v in lines)
	body += f"<tr class='grand'><td>Grand total</td><td class='n'>{_money(grand, doc.currency)}</td></tr>"
	return (
		f"<table class='totals'>{body}</table>"
		f"<p class='words'>{_e(money_in_words(grand, doc.currency))}</p>"
	)


def _dealer_lines(customer: str) -> list[str]:
	c = frappe.db.get_value("Customer", customer, ["customer_name", "custom_gstin", "custom_address", "custom_phone"], as_dict=True) or {}
	return [
		c.get("customer_name") or customer,
		c.get("custom_address"),
		f"GSTIN: {c['custom_gstin']}" if c.get("custom_gstin") else "",
		f"Phone: {c['custom_phone']}" if c.get("custom_phone") else "",
	]


@frappe.whitelist(methods=["GET"])
def render_quotation_html(quotation: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Quotation", quotation)
	table, weight = _sales_lines_table(doc)
	freight = flt(doc.get("custom_freight"))
	notes = (
		f"<p class='notes'>Total weight: {weight:,.1f} kg.</p>" if weight else ""
	) + (f"<p class='notes'>Valid until {formatdate(doc.valid_till)}.</p>" if doc.valid_till else "")
	return _page(
		f"Quotation {doc.name}",
		"Quotation",
		[("No.", doc.name), ("Date", formatdate(doc.transaction_date)), ("Valid till", formatdate(doc.valid_till) if doc.valid_till else "")],
		"Quotation for",
		_dealer_lines(doc.party_name),
		table,
		_sales_totals(doc, freight),
		notes,
	)


@frappe.whitelist(methods=["GET"])
def render_sales_order_html(order: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Sales Order", order)
	table, weight = _sales_lines_table(doc)
	notes = f"<p class='notes'>Total weight: {weight:,.1f} kg.</p>" if weight else ""
	return _page(
		f"Sales Order {doc.name}",
		"Sales Order",
		[
			("No.", doc.name),
			("Date", formatdate(doc.transaction_date)),
			("Delivery date", formatdate(doc.delivery_date) if doc.delivery_date else ""),
			("Your PO", doc.po_no),
		],
		"Customer",
		_dealer_lines(doc.customer),
		table,
		_sales_totals(doc),
		notes,
	)


@frappe.whitelist(methods=["GET"])
def render_purchase_order_html(purchase_order: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Purchase Order", purchase_order)
	s = frappe.db.get_value(
		"Supplier", doc.supplier, ["supplier_name", "custom_address", "custom_phone", "custom_email"], as_dict=True
	) or {}
	supplier_gstin = frappe.db.get_value("Supplier", doc.supplier, "gstin") if frappe.get_meta("Supplier").has_field("gstin") else None
	rows = []
	for i, row in enumerate(doc.items, start=1):
		rows.append(
			f"<tr><td class='n'>{i}</td><td><b>{_e(row.item_code)}</b><br>{_e(row.item_name)}</td>"
			f"<td class='n'>{_qty(row)}</td><td class='n'>{formatdate(row.schedule_date) if row.schedule_date else ''}</td>"
			f"<td class='n'>{_money(row.rate, doc.currency)}</td><td class='n'>{_money(row.amount, doc.currency)}</td></tr>"
		)
	table = (
		"<table><thead><tr><th class='n'>#</th><th>Item</th><th class='n'>Qty</th><th class='n'>Required by</th>"
		"<th class='n'>Rate</th><th class='n'>Amount</th></tr></thead>"
		f"<tbody>{''.join(rows)}</tbody></table>"
	)
	totals = (
		f"<table class='totals'><tr><td>Total</td><td class='n'>{_money(doc.net_total, doc.currency)}</td></tr>"
		+ "".join(
			f"<tr><td>{_e(t.description or t.account_head)}</td><td class='n'>{_money(t.tax_amount, doc.currency)}</td></tr>"
			for t in doc.taxes or []
		)
		+ f"<tr class='grand'><td>Grand total</td><td class='n'>{_money(doc.grand_total, doc.currency)}</td></tr></table>"
	)
	remarks = doc.get("custom_remarks")
	notes = f"<p class='notes'>{_e(remarks)}</p>" if remarks else ""
	return _page(
		f"Purchase Order {doc.name}",
		"Purchase Order",
		[("No.", doc.name), ("Date", formatdate(doc.transaction_date)), ("Required by", formatdate(doc.schedule_date) if doc.schedule_date else "")],
		"Supplier",
		[
			s.get("supplier_name") or doc.supplier,
			s.get("custom_address"),
			f"GSTIN: {supplier_gstin}" if supplier_gstin else "",
			f"Phone: {s['custom_phone']}" if s.get("custom_phone") else "",
			s.get("custom_email"),
		],
		table,
		totals,
		notes,
	)


# ---------------------------------------------------------------------------------------
# Inquiry, Delivery Note, Sales Invoice, Purchase Receipt, Pickup Run / Route Plan and the
# dealer Price List (BRD C.1.3 "weight on every relevant document", C.3.3 bill-to/ship-to,
# C.14.2). Same rule as above: read off the saved document, recompute nothing.
# ---------------------------------------------------------------------------------------


def _plain_lines(text) -> list[str]:
	"""ERPNext keeps addresses as display HTML (`address_display`, `shipping_address`) --
	flatten it to lines, since every value is escaped again on the way into the page."""
	if not text:
		return []
	text = str(text).replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
	return [line.strip() for line in frappe.utils.strip_html_tags(text).splitlines() if line.strip()]


def _weight_kg(item_code: str, qty, batch_no: str | None = None) -> float | None:
	"""Batch weight wins over the item's standard (BRD C.1.3: the variable weight captured at
	inward overrides the standard). None when neither is known, so a blank cell never reads as 0."""
	per_box = None
	if batch_no:
		per_box = frappe.db.get_value("Batch", batch_no, "custom_batch_weight_kg") or None
	per_box = per_box or item_weight_per_box_kg(item_code)
	return flt(per_box) * flt(qty) if per_box and qty else None


def _weight_cell(weight) -> str:
	return f"<td class='n'>{weight:,.1f} kg</td>" if weight else "<td class='n'></td>"


def _total_weight_note(weights: list) -> str:
	"""Only when every line is known: a partial sum would understate the load."""
	if weights and None not in weights and sum(weights):
		return f"<p class='notes'>Total weight: {sum(weights):,.1f} kg.</p>"
	return ""


def _supplier_lines(supplier: str) -> list[str]:
	s = frappe.db.get_value(
		"Supplier", supplier, ["supplier_name", "custom_address", "custom_phone", "custom_email"], as_dict=True
	) or {}
	gstin = frappe.db.get_value("Supplier", supplier, "gstin") if frappe.get_meta("Supplier").has_field("gstin") else None
	return [
		s.get("supplier_name") or supplier,
		s.get("custom_address"),
		f"GSTIN: {gstin}" if gstin else "",
		f"Phone: {s['custom_phone']}" if s.get("custom_phone") else "",
		s.get("custom_email"),
	]


def _bill_ship(doc) -> tuple[list[str], str, list[str]]:
	"""(bill-to lines, ship-to label, ship-to lines). Bill-to falls back to the dealer master
	when the document carries no address; ship-to only appears when it is set, so a plain
	single-address order doesn't print a duplicate block."""
	bill = _plain_lines(doc.get("address_display"))
	dealer = _dealer_lines(doc.customer)
	if not bill:
		bill = dealer
	elif dealer and dealer[0] and dealer[0] not in bill:
		bill = [dealer[0], *bill]
	gstin = doc.get("billing_address_gstin") or doc.get("customer_gstin")
	if gstin and not any(str(line).startswith("GSTIN") for line in bill if line):
		bill = [*bill, f"GSTIN: {gstin}"]
	return bill, "Ship to", _plain_lines(doc.get("shipping_address"))


@frappe.whitelist(methods=["GET"])
def render_inquiry_html(inquiry: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Inquiry", inquiry)
	rows, weights = [], []
	for i, row in enumerate(doc.items, start=1):
		weight = _weight_kg(row.item, row.qty, row.get("preferred_batch_no")) if row.item else None
		weights.append(weight)
		item_name = frappe.db.get_value("Item", row.item, "item_name") if row.item else ""
		style = row.get("custom_requested_style")
		rows.append(
			f"<tr><td class='n'>{i}</td><td><b>{_e(row.item or style)}</b><br>{_e(item_name or '')}</td>"
			f"<td>{_e(style if row.item else '')}</td><td class='n'>{flt(row.qty):g} box</td>"
			f"{_weight_cell(weight)}<td>{_e(row.get('availability_status') or '')}</td></tr>"
		)
	table = (
		"<table><thead><tr><th class='n'>#</th><th>Item</th><th>Requested style</th><th class='n'>Qty</th>"
		"<th class='n'>Weight</th><th>Availability</th></tr></thead>"
		f"<tbody>{''.join(rows)}</tbody></table>"
	)
	notes = _total_weight_note(weights) + (f"<p class='notes'>{_e(doc.remarks)}</p>" if doc.get("remarks") else "")
	contact = " / ".join(p for p in (doc.get("contact_name"), doc.get("contact_phone")) if p)
	return _page(
		f"Inquiry {doc.name}",
		"Inquiry",
		[
			("No.", doc.name),
			("Date", formatdate(doc.date) if doc.date else ""),
			("Status", doc.status),
			("Source", doc.source),
			("Expected delivery", formatdate(doc.expected_delivery) if doc.expected_delivery else ""),
			("Contact", contact),
		],
		"Dealer",
		_dealer_lines(doc.dealer),
		table,
		"",
		notes,
	)


@frappe.whitelist(methods=["GET"])
def render_delivery_note_html(delivery_note: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Delivery Note", delivery_note)
	if doc.docstatus == 0:
		frappe.throw(_("{0} is a draft, not a posted Delivery Note.").format(delivery_note), frappe.DoesNotExistError)
	orders = sorted({r.against_sales_order for r in doc.items if r.get("against_sales_order")})
	rows, weights = [], []
	for i, row in enumerate(doc.items, start=1):
		weight = _weight_kg(row.item_code, row.get("stock_qty") or row.qty, row.get("batch_no"))
		weights.append(weight)
		rows.append(
			f"<tr><td class='n'>{i}</td><td><b>{_e(row.item_code)}</b><br>{_e(row.item_name)}</td>"
			f"<td>{_e(row.get('batch_no') or '')}</td><td class='n'>{_qty(row)}</td>{_weight_cell(weight)}</tr>"
		)
	table = (
		"<table><thead><tr><th class='n'>#</th><th>Item</th><th>Batch</th><th class='n'>Qty</th>"
		"<th class='n'>Weight</th></tr></thead>"
		f"<tbody>{''.join(rows)}</tbody></table>"
	)
	bill, ship_label, ship = _bill_ship(doc)
	cancelled = "<p class='notes'><b>CANCELLED</b></p>" if doc.docstatus == 2 else ""
	return _page(
		f"Delivery Note {doc.name}",
		"Delivery Note",
		[
			("No.", doc.name),
			("Date", formatdate(doc.posting_date)),
			("Order", ", ".join(orders)),
			("Your PO", doc.po_no),
			("Transporter", doc.get("transporter_name")),
			("Vehicle", doc.get("vehicle_no")),
			("LR No.", doc.get("lr_no")),
			("Driver", doc.get("driver_name")),
		],
		"Bill to" if ship else "Customer",
		bill,
		table,
		"",
		cancelled + _total_weight_note(weights),
		ship_label,
		ship,
		receiver_sign="Received by (dealer signature)",
	)


@frappe.whitelist(methods=["GET"])
def render_sales_invoice_html(invoice: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Sales Invoice", invoice)
	if doc.docstatus == 0:
		frappe.throw(_("{0} is a draft, not a posted Sales Invoice.").format(invoice), frappe.DoesNotExistError)
	rows, weights = [], []
	for i, row in enumerate(doc.items, start=1):
		weight = _weight_kg(row.item_code, row.get("stock_qty") or row.qty, row.get("batch_no"))
		weights.append(weight)
		discount = f"{flt(row.discount_percentage):g}%" if flt(row.discount_percentage) else ""
		rows.append(
			f"<tr><td class='n'>{i}</td><td><b>{_e(row.item_code)}</b><br>{_e(row.item_name)}</td>"
			f"<td>{_e(row.get('gst_hsn_code') or '')}</td><td class='n'>{_qty(row)}</td>"
			f"<td class='n'>{_money(row.price_list_rate or row.rate, doc.currency)}</td><td class='n'>{discount}</td>"
			f"<td class='n'>{_money(row.rate, doc.currency)}</td>{_weight_cell(weight)}"
			f"<td class='n'>{_money(row.amount, doc.currency)}</td></tr>"
		)
	table = (
		"<table><thead><tr><th class='n'>#</th><th>Item</th><th>HSN</th><th class='n'>Qty</th>"
		"<th class='n'>List rate</th><th class='n'>Disc.</th><th class='n'>Rate</th><th class='n'>Weight</th>"
		f"<th class='n'>Amount</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
	)
	bill, ship_label, ship = _bill_ship(doc)
	cancelled = "<p class='notes'><b>CANCELLED</b></p>" if doc.docstatus == 2 else ""
	return _page(
		f"Sales Invoice {doc.name}",
		"Tax Invoice",
		[
			("Invoice No.", doc.name),
			("Date", formatdate(doc.posting_date)),
			("Due date", formatdate(doc.due_date) if doc.due_date else ""),
			("Your PO", doc.po_no),
			("Place of supply", doc.get("place_of_supply")),
			("IRN", doc.get("irn")),
			("e-Way bill", doc.get("ewaybill")),
			("Transporter", doc.get("transporter_name")),
			("Vehicle", doc.get("vehicle_no")),
			("LR No.", doc.get("lr_no")),
		],
		"Bill to",
		bill,
		table,
		_sales_totals(doc),
		cancelled + _total_weight_note(weights),
		ship_label,
		ship,
	)


def _bay_split(purchase_receipt: str) -> dict[tuple[str, str], dict]:
	"""(item, batch) -> {bays, shortage, damaged} from the Bay Allocation(s) made for this receipt."""
	out: dict[tuple[str, str], dict] = {}
	for a in frappe.get_all(
		"Bay Allocation",
		filters={"purchase_receipt": purchase_receipt},
		fields=["name", "item", "batch_no", "shortage_qty", "damaged_qty"],
	):
		slot = out.setdefault((a.item, a.batch_no or ""), {"bays": [], "shortage": 0.0, "damaged": 0.0})
		slot["shortage"] += flt(a.shortage_qty)
		slot["damaged"] += flt(a.damaged_qty)
		for line in frappe.get_all("Bay Allocation Line", filters={"parent": a.name}, fields=["bay", "qty"]):
			slot["bays"].append(f"{line.bay} ({flt(line.qty):g})")
	return out


@frappe.whitelist(methods=["GET"])
def render_purchase_receipt_html(purchase_receipt: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Purchase Receipt", purchase_receipt)
	if doc.docstatus == 0:
		frappe.throw(_("{0} is a draft, not a posted Purchase Receipt.").format(purchase_receipt), frappe.DoesNotExistError)
	split = _bay_split(doc.name)
	orders = sorted({r.purchase_order for r in doc.items if r.get("purchase_order")})
	rows, weights = [], []
	for i, row in enumerate(doc.items, start=1):
		info = split.get((row.item_code, row.get("batch_no") or ""), {})
		weight = _weight_kg(row.item_code, row.get("received_stock_qty") or row.qty, row.get("batch_no"))
		weights.append(weight)
		rows.append(
			f"<tr><td class='n'>{i}</td><td><b>{_e(row.item_code)}</b><br>{_e(row.item_name)}</td>"
			f"<td>{_e(row.get('batch_no') or '')}</td><td class='n'>{flt(row.received_qty or row.qty):g}</td>"
			f"<td class='n'>{flt(info.get('shortage')):g}</td>"
			f"<td class='n'>{flt(row.rejected_qty) + flt(info.get('damaged')):g}</td>"
			f"<td>{_e(', '.join(info.get('bays', [])))}</td>{_weight_cell(weight)}</tr>"
		)
	table = (
		"<table><thead><tr><th class='n'>#</th><th>Item</th><th>Batch</th><th class='n'>Received</th>"
		"<th class='n'>Shortage</th><th class='n'>Damaged</th><th>Bay</th><th class='n'>Weight</th></tr></thead>"
		f"<tbody>{''.join(rows)}</tbody></table>"
	)
	cancelled = "<p class='notes'><b>CANCELLED</b></p>" if doc.docstatus == 2 else ""
	return _page(
		f"Purchase Receipt {doc.name}",
		"Purchase Receipt",
		[
			("No.", doc.name),
			("Date", formatdate(doc.posting_date)),
			("Purchase Order", ", ".join(orders)),
			("Supplier invoice", doc.get("custom_supplier_invoice_no")),
			("Invoice date", formatdate(doc.custom_supplier_invoice_date) if doc.get("custom_supplier_invoice_date") else ""),
			("Supplier delivery note", doc.get("supplier_delivery_note")),
			("LR No.", doc.get("lr_no")),
			("Transporter", doc.get("transporter_name")),
		],
		"Received from",
		_supplier_lines(doc.supplier),
		table,
		"",
		cancelled + _total_weight_note(weights),
		receiver_sign="Received by (warehouse)",
	)


def _plan_lines_table(lines, show_supplier: bool = False) -> tuple[str, float | None]:
	rows, weights = [], []
	for i, row in enumerate(lines, start=1):
		weight = _weight_kg(row.item, row.qty)
		weights.append(weight)
		item_name = frappe.db.get_value("Item", row.item, "item_name") or ""
		supplier = (
			f"<td>{_e(frappe.db.get_value('Supplier', row.supplier, 'supplier_name') or row.supplier or '')}</td>"
			if show_supplier
			else ""
		)
		rows.append(
			f"<tr><td class='n'>{i}</td>{supplier}<td>{_e(row.purchase_order)}</td>"
			f"<td><b>{_e(row.item)}</b><br>{_e(item_name)}</td><td class='n'>{flt(row.qty):g} box</td>"
			f"{_weight_cell(weight)}</tr>"
		)
	head = "<th>Supplier</th>" if show_supplier else ""
	table = (
		f"<table><thead><tr><th class='n'>#</th>{head}<th>PO</th><th>Item</th><th class='n'>Qty</th>"
		f"<th class='n'>Weight</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
	)
	return table, (sum(weights) if weights and None not in weights else None)


@frappe.whitelist(methods=["GET"])
def render_pickup_run_html(pickup_run: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Pickup Run", pickup_run)
	table, weight = _plan_lines_table(doc.lines)
	notes = f"<p class='notes'>Total weight: {weight:,.1f} kg.</p>" if weight else ""
	return _page(
		f"Pickup Run {doc.name}",
		"Pickup Plan",
		[
			("No.", doc.name),
			("Pickup date", formatdate(doc.scheduled_date) if doc.scheduled_date else ""),
			("Status", doc.status),
			("Vehicle type", doc.vehicle_type),
			("Vehicle no.", doc.vehicle_number),
			("Total boxes", f"{doc.total_boxes}" if doc.total_boxes else ""),
		],
		"Collect from",
		_supplier_lines(doc.supplier),
		table,
		"",
		notes,
	)


@frappe.whitelist(methods=["GET"])
def render_route_plan_html(route_plan: str) -> str:
	_assert_can_print()
	doc = frappe.get_doc("Route Plan", route_plan)
	stops = []
	for s in sorted(doc.stops, key=lambda r: r.sequence_no or 0):
		lines = _supplier_lines(s.supplier)
		address = "<br>".join(_e(line) for line in lines[1:] if line)
		leg = f"{flt(s.leg_distance_km):,.1f} km" if s.leg_distance_km else ""
		mins = f"{s.leg_duration_minutes} min" if s.leg_duration_minutes else ""
		stops.append(
			f"<tr><td class='n'>{s.sequence_no}</td><td><b>{_e(lines[0])}</b><br>{address}</td>"
			f"<td class='n'>{leg}</td><td class='n'>{mins}</td></tr>"
		)
	stops_table = (
		"<table><thead><tr><th class='n'>Stop</th><th>Supplier</th><th class='n'>Leg distance</th>"
		f"<th class='n'>Leg time</th></tr></thead><tbody>{''.join(stops)}</tbody></table>"
	)
	lines_table, weight = _plan_lines_table(doc.lines, show_supplier=True)
	weight = doc.total_weight_kg or weight
	notes = f"<p class='notes'>Total weight: {weight:,.1f} kg.</p>" if weight else ""
	return _page(
		f"Route Plan {doc.name}",
		"Pickup Plan",
		[
			("No.", doc.name),
			("Pickup date", formatdate(doc.scheduled_date) if doc.scheduled_date else ""),
			("Status", doc.status),
			("Vehicle", doc.transporter_vehicle),
			("Vehicle kind", doc.vehicle_kind),
			("Distance", f"{flt(doc.total_distance_km):,.1f} km" if doc.total_distance_km else ""),
			("Drive time", f"{doc.total_duration_minutes} min" if doc.total_duration_minutes else ""),
		],
		"Transporter",
		[doc.transporter],
		stops_table + "<p class='label' style='margin-top:18px'>Goods to collect</p>" + lines_table,
		"",
		notes,
	)


@frappe.whitelist(methods=["GET"])
def render_dealer_price_list_html(dealer: str) -> str:
	"""BRD C.14.2: the dealer's own list -- only the items their catalog shows, at the rate
	pricing.get_price_for_dealer resolves for them (tagged list, else their tier, else the flat
	Dealer list), so one dealer's printout never carries another tier's number. Staff only, for
	the same reason as dealer_catalog_export. An MRP column appears only when the site has a
	Price List named "MRP"; nothing is guessed when it doesn't."""
	from dms_erp.auth.api import STAFF_ROLES
	from dms_erp.catalog.dealer_catalog_api import catalog_for
	from dms_erp.pricing.api import get_dealer_price, get_price_for_dealer

	if not set(frappe.get_roles(frappe.session.user)) & set(STAFF_ROLES):
		frappe.throw(_("Only staff can print a dealer's price list."), frappe.PermissionError)
	codes = catalog_for(dealer)
	items = (
		frappe.get_all(
			"Item",
			filters={"name": ["in", codes]},
			fields=["name", "item_name", "custom_size", "custom_finish", "custom_series", "item_group"],
			order_by="item_group asc, item_name asc",
		)
		if codes
		else []
	)
	tagged = frappe.db.get_value("Customer", dealer, "default_price_list")
	tier = tagged or frappe.db.get_value("Customer", dealer, "custom_dealer_classification") or "Standard Dealer"
	with_mrp = bool(frappe.db.exists("Price List", "MRP"))
	rows = []
	for it in items:
		rate = get_price_for_dealer(it.name, dealer)
		mrp = get_dealer_price(it.name, "MRP") if with_mrp else None
		mrp_cell = f"<td class='n'>{_money(mrp) if mrp is not None else '—'}</td>" if with_mrp else ""
		rows.append(
			f"<tr><td><b>{_e(it.name)}</b></td><td>{_e(it.item_name)}</td><td>{_e(it.custom_series or '')}</td>"
			f"<td>{_e(it.custom_size or '')} {_e(it.custom_finish or '')}</td>"
			f"<td class='n'>{_money(rate) if rate is not None else '—'}</td>{mrp_cell}</tr>"
		)
	if not rows:
		rows = [f"<tr><td colspan='{6 if with_mrp else 5}'>No items are visible in this dealer's catalog.</td></tr>"]
	mrp_head = "<th class='n'>MRP / box</th>" if with_mrp else ""
	table = (
		"<table><thead><tr><th>Item code</th><th>Name</th><th>Series</th><th>Size / Finish</th>"
		f"<th class='n'>Price / box</th>{mrp_head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
	)
	return _page(
		f"Price List {dealer}",
		"Price List",
		[("Price list", tier), ("Date", formatdate(frappe.utils.nowdate()))],
		"Prepared for",
		_dealer_lines(dealer),
		table,
		"",
		"<p class='notes'>Prices are per box.</p>",
	)
