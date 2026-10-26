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
	.sign { margin-top: 48px; display: flex; justify-content: flex-end; }
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
		  table: str, totals: str, notes: str = "") -> str:
	company_name, company_lines = _company_block()
	meta = "".join(f"<p>{_e(k)}: <b>{_e(v)}</b></p>" for k, v in doc_lines if v)
	party = "".join(f"<p>{_e(line)}</p>" for line in party_lines if line)
	return (
		f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)}</title><style>{_CSS}</style></head><body>"
		f"<div class='head'><div class='co'><h1>{_e(company_name)}</h1>{company_lines}</div>"
		f"<div class='doc'><h2>{_e(doc_label)}</h2>{meta}</div></div>"
		f"<div class='parties'><div><div class='label'>{_e(party_label)}</div>{party}</div></div>"
		f"{table}{totals}{notes}"
		f"<div class='sign'><div>For {_e(company_name)}</div></div>"
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
