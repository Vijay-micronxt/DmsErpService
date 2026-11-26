"""Tally Sales / Delivery Note / Purchase / receipt / payment / contra vouchers -> DRAFT ERP documents (24-25 Nov 2026).

The history was migrated up to 21 Aug 2026 as Delivery Notes, Sales Invoices, Purchase Invoices
and Stock Entries, each tagged with its Tally voucher id (tally_guid). This carries on from there
and builds the same documents the same way, as DRAFTS a person checks and submits:

- Tally Delivery Note  -> Delivery Note          (stock goes OUT here, as in Tally)
- Tally Sales          -> Sales Invoice          (update_stock off when it was raised against a
                                                   delivery note -- its lines carry that note's
                                                   tracking number and the stock already left --
                                                   on when it is a direct sale)
- Tally Purchase, Purchase (New), Purchase Transit
                       -> Purchase Invoice       (update_stock on: stock comes IN)

Copied from how the migration shaped them (checked on the migrated documents): rates are the line
amount over its quantity so totals tie to Tally; Tally's tax, round-off and freight ledgers become
fixed-amount tax rows on the ledger's own Account, with Tally's sign (as is on a sale, reversed on
a purchase); the sales / purchase ledger of each line is its income / expense account.

A purchase may bring what the ERP lacks, so there (and only there) a missing Item is created, with
the Tally stock group as its Item Group, and a missing Batch is created for the line's lot. Both
are returned in `newItems` / `newBatches` for review. Everything else missing -- a customer, a
supplier, a godown, an account, or an item or batch on a SALE -- is reported, never invented: one
voucher is one draft, all or nothing, and a half-made invoice would misstate the books more
quietly than a missing one. Receipts, payments and contra go through tally_money.py (Payment Entry
or Journal Entry). Credit and debit notes are Sales / Purchase Invoice returns (_build_return); stock journals are
counted, not handled yet.
"""

import datetime
import html
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from dms_erp.sales.tally_sync import SYNC_ROLES, piece_unit

# Off until Pacific signs this off; a site can turn it on alone:
#   bench --site <site> set-config dms_erp_tally_vouchers_enabled 1
TALLY_VOUCHERS_ENABLED = False

MAX_VOUCHERS = 200
# Doctypes the migration (and this) tag with tally_guid -- a voucher in any of them is already here.
GUID_DOCTYPES = ("Stock Entry", "Delivery Note", "Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry")

PURCHASE_TYPES = ("Purchase", "Purchase (New)", "Purchase Transit")
RECEIPT_TYPES = ("Bank Receipt", "Cash Receipt")
PAYMENT_TYPES = ("Bank Payment", "Cash Payment", "Payment")
CONTRA_TYPES = ("Contra",)
MONEY_TYPES = RECEIPT_TYPES + PAYMENT_TYPES + CONTRA_TYPES
# Tally's own spellings, including the stray space it carries in "Credit Note ( Purchase)". As the
# migration did: a credit note, of either kind, is a Sales Invoice return; a debit note, of either
# kind, a Purchase Invoice return -- whichever side of the business Tally filed it under.
CREDIT_TYPES = ("Credit Note", "Credit Note (Purchase)", "Credit Note ( Purchase)")
DEBIT_TYPES = ("Debit Note", "Debit Note (Sales)")
RETURN_TYPES = CREDIT_TYPES + DEBIT_TYPES
LEDGER_LINE_ITEM = "TALLY-LEDGER-LINE"
TAX_ACCOUNT_TYPES = ("Tax", "Round Off")
HANDLED_TYPES = ("Delivery Note", "Sales") + PURCHASE_TYPES + MONEY_TYPES + RETURN_TYPES
# What Tally puts in a tracking-number slot that tracks nothing.
NO_TRACKING = {"", "not applicable"}
FALLBACK_ITEM_GROUP = "Tally Imported"


class TallyVoucherError(frappe.ValidationError):
	pass


def parse_tally_qty(text, item_code: str, stock_uom: str) -> float:
	"""Tally's quantity text ("3 Box 1 peices", " 12 Slabs") as stock-UOM units: the reverse of
	sales.tally_sync.tally_qty. A unit that isn't the item's stock unit or its piece unit is refused --
	guessing would put a wrong figure on an invoice."""
	text = (text or "").strip()
	numbers = re.findall(r"-?\d[\d,]*\.?\d*|-?\.\d+", text)
	units = re.findall(r"[^\W\d_][\w.]*", text)
	try:
		values = [float(n.replace(",", "")) for n in numbers]
	except ValueError:
		values = []
	if not values or len(values) != len(units) or len(values) > 2:
		raise TallyVoucherError(_("Can't read the Tally quantity {0!r}.").format(text))
	uom = stock_uom.lower()
	pieces = piece_unit(item_code, stock_uom)
	if len(values) == 2:
		if units[0].lower() != uom or not pieces or units[1].lower() != pieces[0].lower():
			raise TallyVoucherError(_("{0!r} doesn't match {1}'s units ({2}).").format(text, item_code, stock_uom))
		return values[0] + values[1] / pieces[1]
	if units[0].lower() == uom:
		return values[0]
	if pieces and units[0].lower() == pieces[0].lower():
		return values[0] / pieces[1]
	raise TallyVoucherError(_("{0!r} isn't in {1}'s stock unit ({2}).").format(text, item_code, stock_uom))


def resolve_warehouse(godown: str, company: str, cache: dict):
	"""The leaf Warehouse for a Tally godown: the migration named warehouses after godowns, and gave
	a godown that holds stock AND has children a "<name> (Main)" leaf."""
	key = (godown, company)
	if key not in cache:
		rows = frappe.get_all(
			"Warehouse",
			filters={"company": company, "is_group": 0, "warehouse_name": ["in", [godown, f"{godown} (Main)"]]},
			fields=["name", "warehouse_name"],
		)
		exact = [r.name for r in rows if r.warehouse_name == godown]
		cache[key] = (exact or [r.name for r in rows] or [None])[0]
	return cache[key]


def resolve_batch(label: str, item_code: str):
	"""The ERP Batch for a Tally batch label: the label itself, or "<label> - <item>" when two items
	shared it, or the stored original label."""
	name = frappe.db.get_value("Batch", {"item": item_code, "name": ["in", [label, f"{label} - {item_code}"]]}, "name")
	if not name and frappe.get_meta("Batch").has_field("tally_batch_name"):
		name = frappe.db.get_value("Batch", {"item": item_code, "tally_batch_name": label}, "name")
	return name


def _assert_enabled():
	if not (TALLY_VOUCHERS_ENABLED or cint(frappe.conf.get("dms_erp_tally_vouchers_enabled"))):
		frappe.throw(_("Tally voucher import is not enabled on this site yet."), frappe.ValidationError)


def _clean(text) -> str:
	"""Tally's own text with its control characters taken out. Tally sends "&#4; Not Applicable",
	which arrives either still escaped or already decoded to a control character, depending on who
	read it, so both are handled."""
	return re.sub(r"[\x00-\x1f]", "", html.unescape(str(text or ""))).strip()


def known_guids(guids: list[str]) -> set[str]:
	"""Which of these Tally voucher ids the ERP already holds, in any document type."""
	found: set[str] = set()
	guids = [g for g in guids if g]
	for doctype in GUID_DOCTYPES:
		if not frappe.get_meta(doctype).has_field("tally_guid"):
			continue
		for start in range(0, len(guids), 1000):
			found.update(
				frappe.get_all(doctype, filters={"tally_guid": ["in", guids[start : start + 1000]]}, pluck="tally_guid", ignore_permissions=True)
			)
	return found


@frappe.whitelist(methods=["POST"])
def filter_new_tally_vouchers(guids):
	"""The subset of `guids` the ERP does not hold yet -- what the agent still has to read in full.
	Read-only, so it also works before the import is switched on (that is what a dry run uses)."""
	frappe.only_for(SYNC_ROLES)
	if isinstance(guids, str):
		guids = json.loads(guids)
	if not isinstance(guids, list):
		frappe.throw(_("guids must be a list."), frappe.ValidationError)
	held = known_guids(guids)
	return [g for g in guids if g not in held]


@frappe.whitelist(methods=["POST"])
def receive_tally_vouchers(vouchers, company: str | None = None):
	"""vouchers: [{"guid", "type", "number", "date" (YYYYMMDD), "party", "reference", "narration",
	"cancelled", "ledgers": [{"ledger", "amount", "party"}], "rows": [{"item", "item_group", "hsn",
	"godown", "batch", "quantity" (Tally's text), "amount", "tracking", "account"}]}].
	Makes one draft document per voucher; returns what it made, left alone, and could not place."""
	_assert_enabled()
	frappe.only_for(SYNC_ROLES)
	if isinstance(vouchers, str):
		vouchers = json.loads(vouchers)
	if not isinstance(vouchers, list) or not vouchers:
		frappe.throw(_("No vouchers received."), frappe.ValidationError)
	if len(vouchers) > MAX_VOUCHERS:
		frappe.throw(_("Too many vouchers in one request ({0}, max {1}).").format(len(vouchers), MAX_VOUCHERS), frappe.ValidationError)
	company = company or frappe.defaults.get_global_default("company")
	if not company:
		frappe.throw(_("No company to import vouchers for."), frappe.ValidationError)
	return _build_drafts(vouchers, company)


# ---------------------------------------------------------------- masters


def _party(doctype: str, name: str):
	"""The Customer / Supplier for a Tally party ledger: the migration named them after the ledger."""
	field = "customer_name" if doctype == "Customer" else "supplier_name"
	return frappe.db.get_value(doctype, {field: name}, "name") or (name if frappe.db.exists(doctype, name) else None)


def _address(doctype: str, party: str, kind: str):
	"""The party's Billing (or Shipping) address, taken only from addresses actually linked to THIS
	customer or supplier: a name like "<party>-Billing" can belong to the same party as a customer
	when it is also a supplier, and ERPNext refuses an invoice carrying the other one's address."""
	links = frappe.get_all("Dynamic Link", filters={"link_doctype": doctype, "link_name": party, "parenttype": "Address"}, pluck="parent")
	if not links:
		return None
	return frappe.db.get_value("Address", {"name": ["in", links], "address_type": kind}, "name") or frappe.db.get_value("Address", {"name": ["in", links]}, "name")


def _account(ledger: str, company: str):
	ledger = _clean(ledger)
	abbr = frappe.get_cached_value("Company", company, "abbr")
	for name in (f"{ledger} - {abbr}", ledger):
		if frappe.db.exists("Account", {"name": name, "company": company}):
			return name
	return frappe.db.get_value("Account", {"account_name": ledger, "company": company}, "name")


def _posting_account(ledger: str, company: str):
	"""The Account a Tally ledger posts to. Where the chart has a group of the same name (Pacific's
	"Loading Charges"), the migration booked to its leaf "<name> (Ledger)", because ERPNext refuses a
	posting to a group; do the same, and stop (None) rather than post to the group."""
	account = _account(ledger, company)
	if account and frappe.db.get_value("Account", account, "is_group"):
		return _account(f"{_clean(ledger)} (Ledger)", company)
	return account


def _ensure_hsn(code: str):
	"""The GST HSN Code for what Tally printed on the line, created when the master lacks it. India
	Compliance wants 6 or 8 digits on anything that is sold, so a 4-digit code (Pacific's tiles are
	"6907" in Tally) is not used: None, and the item is made purchase-only (see _ensure_item)."""
	code = _clean(code)
	if not re.fullmatch(r"\d{6}|\d{8}", code):
		return None
	if not frappe.db.exists("GST HSN Code", code):
		frappe.get_doc({"doctype": "GST HSN Code", "hsn_code": code, "description": "From a Tally purchase voucher"}).insert(ignore_permissions=True)
	return code


def _ensure_item(row: dict, new_items: list) -> str:
	"""The Item for a purchase line, created when the ERP lacks it (see the module docstring)."""
	item = _clean(row.get("item"))
	if not item:
		raise TallyVoucherError(_("A line has no item name."))
	if frappe.db.exists("Item", item):
		return item
	group = _clean(row.get("item_group")) or FALLBACK_ITEM_GROUP
	if not frappe.db.exists("Item Group", group):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": group, "parent_item_group": "All Item Groups", "is_group": 0}).insert(
			ignore_permissions=True
		)
	hsn = _ensure_hsn(row.get("hsn"))
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item,
			"item_name": item,
			"item_group": group,
			"stock_uom": "Box",
			"is_stock_item": 1,
			"has_batch_no": 1,
			"create_new_batch": 0,
			"gst_hsn_code": hsn,
			# India Compliance wants a valid HSN on anything sold; without one it is bought, not sold,
			# until a person adds it (it is listed in newItems as needsHsn).
			"is_sales_item": 1 if hsn else 0,
			"description": f"Created from a Tally purchase voucher; check the group, HSN and pieces per box. Tally group: {group}.",
		}
	)
	# Tiles are bought by the box and sold in pieces: "... 09 Pcs" in the name is the pieces per box,
	# and the "peices" conversion is what lets a quantity like "3 Box 4 peices" be read.
	per_box = re.search(r"(\d+)\s*(?:pcs?|pc)\.?\s*$", item, re.IGNORECASE)
	if per_box and cint(per_box.group(1)) > 1 and frappe.db.exists("UOM", "peices"):
		doc.append("uoms", {"uom": "Box", "conversion_factor": 1})
		doc.append("uoms", {"uom": "peices", "conversion_factor": 1 / cint(per_box.group(1))})
	doc.insert(ignore_permissions=True)
	new_items.append({"item": item, "group": group, "hsn": hsn, "needsHsn": not hsn})
	return item


def _ensure_batch(label: str, item: str, new_batches: list) -> str:
	"""The Batch for a purchase line's lot, created when the ERP lacks it. Named like the
	migration's: the label, or "<label> - <item>" when another item already uses it."""
	name = resolve_batch(label, item)
	if name:
		return name
	batch_id = label if not frappe.db.exists("Batch", label) else f"{label} - {item}"
	values = {"doctype": "Batch", "batch_id": batch_id, "item": item}
	if frappe.get_meta("Batch").has_field("tally_batch_name"):
		values["tally_batch_name"] = label
	frappe.get_doc(values).insert(ignore_permissions=True)
	new_batches.append({"batch": batch_id, "item": item})
	return batch_id


# ---------------------------------------------------------------- one voucher -> one document


def _whole_units(text) -> str:
	"""Tally writes "60 Box 0 peices" even for an item with no pieces-per-box; with no loose pieces it
	is simply 60 Box, which any item can read."""
	m = re.fullmatch(r"\s*(-?[\d,.]+)\s+(\S+)\s+0\s+\S+\s*", _clean(text))
	return f"{m.group(1)} {m.group(2)}" if m else text


def _boxes_and_pieces(text) -> str:
	"""Tally's own quantity as a person reads it -- "16 Box 3 Pcs" -- for the line's text field. The
	Qty column itself is a decimal (3.3333 Box), which is what stock is counted in but is hard to read
	against a stack of boxes and loose tiles."""
	m = re.fullmatch(r"(-?[\d,.]+)\s+(\S+)\s+(\d+)\s+\S+", _clean(text))
	return f"{m.group(1)} {m.group(2)} {m.group(3)} Pcs" if m else _clean(text)


def _line(row: dict, voucher: dict, company: str, cache: dict, creating: bool, new_items: list, new_batches: list, sign: int = 1) -> dict:
	item = _ensure_item(row, new_items) if creating else _clean(row.get("item"))
	if not item or not frappe.db.exists("Item", item):
		raise TallyVoucherError(_("Item not in the ERP: {0}").format(item or "(blank)"))
	has_batch, stock_uom = frappe.get_cached_value("Item", item, ["has_batch_no", "stock_uom"])
	godown = _clean(row.get("godown"))
	warehouse = resolve_warehouse(godown, company, cache)
	if not warehouse:
		raise TallyVoucherError(_("Godown not in the ERP: {0}").format(godown or "(blank)"))
	qty = parse_tally_qty(_whole_units(row.get("quantity")), item, stock_uom)
	if qty <= 0:
		raise TallyVoucherError(_("{0}: quantity {1} is not a movement.").format(item, _clean(row.get("quantity"))))
	out = {
		"item_code": item,
		"qty": sign * qty,
		"uom": stock_uom,
		"conversion_factor": 1,
		"warehouse": warehouse,
		# What the line really came to, so the invoice ties to Tally's own total.
		"rate": abs(flt(row.get("amount"))) / qty,
		# Boxes and loose pieces as Tally shows them, next to the decimal Qty (the migration's field).
		"tally_qty_text": _boxes_and_pieces(row.get("quantity")),
		"tally_qty_value": qty,
		"allow_zero_valuation_rate": 1,
		**_row_tally_fields(row),
	}
	if cint(has_batch):
		label = _clean(row.get("batch"))
		if not label:
			raise TallyVoucherError(_("Batch missing on {0}.").format(item))
		batch = _ensure_batch(label, item, new_batches) if creating else resolve_batch(label, item)
		if not batch:
			raise TallyVoucherError(_("Batch not in the ERP: {0} / {1}").format(item, label))
		out.update({"batch_no": batch, "use_serial_batch_fields": 1})
	ledger = _clean(row.get("account"))
	if ledger and voucher["type"] != "Delivery Note":
		account = _posting_account(ledger, company)
		if not account:
			raise TallyVoucherError(_("Account not in the ERP: {0}").format(ledger))
		out["expense_account" if voucher["type"] in PURCHASE_TYPES + DEBIT_TYPES else "income_account"] = account
	return out


def _taxes(voucher: dict, company: str, purchase: bool, only_tax: bool = False) -> list[dict]:
	"""Tally's non-party ledger lines as fixed-amount tax rows. Tally books a sale's taxes as
	credits (positive) and a purchase's as debits (negative); the ERP wants them positive on both.
	A ledger some stock line is booked to (a credit note lists its sales-return ledger among the
	ledgers, as well as on the line) counts only for what the lines do not already carry.
	`only_tax`: for a ledger-only note, where every other ledger is a line, keep just the Tax and
	Round Off accounts."""
	rows = []
	booked: dict[str, float] = {}
	for r in voucher.get("rows") or []:
		if _clean(r.get("account")):
			booked[_clean(r.get("account"))] = booked.get(_clean(r.get("account")), 0) + flt(r.get("amount"))
	for ledger in voucher.get("ledgers") or []:
		amount = flt(ledger.get("amount"))
		if ledger.get("party") or not amount:
			continue
		name = _clean(ledger.get("ledger"))
		if name in booked:
			# Tally lists the line account among the ledgers too, so the lines' share is not a charge;
			# what is left over (a debit note's extra "Damages Claim" on top of its stock lines, listed
			# as a second entry for the same ledger) is. The lines' total is used up entry by entry.
			used = amount if abs(amount) <= abs(booked[name]) + 0.005 else booked[name]
			booked[name] -= used
			amount = round(amount - used, 2)
			if abs(amount) < 0.005:
				continue
		account = _account(ledger.get("ledger"), company)
		if only_tax and account and frappe.db.get_value("Account", account, "account_type") not in TAX_ACCOUNT_TYPES:
			continue
		if not account:
			raise TallyVoucherError(_("Account not in the ERP: {0}").format(_clean(ledger.get("ledger"))))
		rows.append(
			{
				"charge_type": "Actual",
				"account_head": account,
				"description": _clean(ledger.get("ledger")),
				"tax_amount": -amount if purchase else amount,
			}
		)
	return rows


# What the agent reads off a voucher's e-invoice and transport details, and the field the migration
# put each in on the ERP document. Only a field the document type really has is set (a Delivery
# Note has no IRN, say), and a value Tally left blank is left blank -- never a placeholder.
TRANSPORT_FIELDS = {
	"vehicle": "tally_vehicle_no",
	"eway_bill": "tally_eway_bill_no",
	"irn": "tally_irn",
	"irn_ack_no": "tally_irn_ack_no",
	"irn_ack_date": "tally_irn_ack_date",
	"entered_by": "tally_entered_by",
	"transporter": "tally_transporter",
	"lr_no": "tally_lr_no",
	"lr_date": "tally_lr_date",
	"driver_name": "tally_driver_name",
	"driver_phone": "tally_driver_phone",
	"driver_licence": "tally_driver_licence",
	"vehicle_owner": "tally_vehicle_owner",
	"vehicle_owner_pan": "tally_vehicle_owner_pan",
	"transporter_pan": "tally_transporter_pan",
	"transporter_gstin": "tally_transporter_gstin",
	"vehicle_permit": "tally_vehicle_permit",
}


def _tally_datetime(text):
	"""Tally's "8-Aug-26 at 12:32" as a datetime, or None when it is not that."""
	try:
		return datetime.datetime.strptime(_clean(text), "%d-%b-%y at %H:%M")
	except ValueError:
		return None


def _party_bill_name(voucher: dict, invoice: bool):
	"""Tally's name for this document as a bill, which later receipts and payments settle: the party
	line's "New Ref" bill. An invoice or note with no such bill (a credit note allocated against the invoice
	it reverses carries that invoice's name as an "Agst Ref", which is not its own) is its voucher number."""
	for line in voucher.get("ledgers") or []:
		if line.get("party"):
			for bill in line.get("bills") or []:
				if _clean(bill.get("type")).lower() == "new ref" and _clean(bill.get("name")):
					return _clean(bill["name"])
	return (voucher.get("number") or None) if invoice else None


def _tally_fields(voucher: dict, doctype: str) -> dict:
	"""The Tally reference, transport and audit fields, as the migration filled them: who entered and
	approved the voucher and when, every alteration with its reason, the custom (UDF) values by name,
	and the bill name. Only a field the document type really has is set; a blank is left blank."""
	meta = frappe.get_meta(doctype)
	audit = voucher.get("audit") or {}
	fields = {
		"tally_guid": voucher["guid"],
		"tally_voucher_type": voucher["type"],
		"tally_voucher_no": voucher["number"],
		"tally_reference": _clean(voucher.get("reference")) or None,
	}
	for key, value in (voucher.get("transport") or {}).items():
		field = TRANSPORT_FIELDS.get(key)
		if field and _clean(value) and meta.has_field(field):
			fields[field] = _clean(value)
	# Who entered it is Pacific's own "entered by" custom field; Tally's ENTEREDBY is whoever saved it last.
	if audit.get("entered_by") and meta.has_field("tally_entered_by"):
		fields["tally_entered_by"] = _clean(audit["entered_by"])
	for key, field in (("entered_on", "tally_entered_on"), ("approved_on", "tally_approved_on")):
		moment = _tally_datetime(audit.get(key))
		if moment and meta.has_field(field):
			fields[field] = moment
	if audit.get("approved_by") and meta.has_field("tally_approved_by"):
		fields["tally_approved_by"] = _clean(audit["approved_by"])
	if audit.get("classification") and meta.has_field("tally_customer_classification"):
		fields["tally_customer_classification"] = _clean(audit["classification"])
	if audit.get("udf") and meta.has_field("tally_udf"):
		fields["tally_udf"] = json.dumps(audit["udf"], ensure_ascii=False)
	if audit.get("alterations") and meta.has_field("tally_alterations"):
		fields["tally_alterations"] = [
			{"log_no": cint(a.get("log_no")), "user": _clean(a.get("user")), "altered_on": _clean(a.get("altered_on")), "reason": _clean(a.get("reason")) or None}
			for a in audit["alterations"]
		]
	bill = _party_bill_name(voucher, invoice=doctype in ("Sales Invoice", "Purchase Invoice"))
	if meta.has_field("tally_bill_no") and bill:
		fields["tally_bill_no"] = bill
	return fields


def _row_tally_fields(row: dict) -> dict:
	"""The line's own Tally values (Pacific's custom item fields, the rate and discount printed on it),
	in the fields the migration used. Per batch where Tally has it per batch."""
	udf = row.get("udf") or {}
	out = {}

	def number(text):
		m = re.match(r"\s*(-?[\d,]*\.?\d+)", _clean(text))
		return flt(m.group(1).replace(",", "")) if m else None

	for field, value in (
		("tally_sq_mtr", number(udf.get("PIITEMBTACHSQMTR") or udf.get("PIITEMSQMTR"))),
		("tally_weight", number(udf.get("PIITEMBATCHWEIGHT") or udf.get("PIITEMWEIGHT"))),
		("tally_rate", number(row.get("rate"))),
		("tally_discount", number(row.get("discount"))),
	):
		if value is not None:
			out[field] = value
	for field, value in (
		("tally_item_type", udf.get("PIITEMTYPE") or udf.get("PIBATCHITEMTYPE")),
		("tally_reservation_status", udf.get("PIVCHRESERVEDSTATUS")),
	):
		if _clean(value):
			out[field] = _clean(value)
	if udf:
		out["tally_udf"] = json.dumps(udf, ensure_ascii=False)
	return out


def _posting_time(vtype: str) -> str:
	"""Within a day the migration posted purchases first, then delivery notes, then sales (01:00:00, then a
	few seconds later each), so what comes in is always on the books before what goes out of it. A
	draft given the same time as everything else, or "now", could land ahead of the stock it relies on
	and be refused as negative stock. Credit notes bring stock back, so they sit with the inflows;
	debit notes take it out, so they go last."""
	rank = 0 if vtype in PURCHASE_TYPES else 1 if vtype in CREDIT_TYPES else 2 if vtype == "Delivery Note" else 3 if vtype == "Sales" else 4
	return f"01:00:{rank:02d}"


def _document_defaults(doctype: str, party_type: str, party: str, vtype: str = "") -> dict:
	"""What the migrated documents carry on every invoice and delivery note: Tally's own round-off is the
	only rounding, Tally's prices are final, and a time that puts the day's entries ahead of anything
	typed that day. The e-invoice / e-way bill status is "Not Applicable" as the migration set it: the
	IRN and e-way bill number are kept in their own Tally fields."""
	meta = frappe.get_meta(doctype)
	out = {"posting_time": _posting_time(vtype), "disable_rounded_total": 1, "ignore_pricing_rule": 1}
	if meta.has_field("customer_group") and party_type == "Customer":
		out["customer_group"] = frappe.db.get_value("Customer", party, "customer_group")
	for field in ("einvoice_status", "e_waybill_status", "reconciliation_status"):
		if meta.has_field(field):
			out[field] = "Not Applicable"
	return out


def _insert(doc: dict):
	"""The one place a draft is created. India Compliance defaults the LR date to today; a voucher
	with no LR date has none, not the day it was imported."""
	document = frappe.get_doc(doc)
	has_lr = document.meta.has_field("lr_date")
	if has_lr:
		document.lr_date = None
	document.insert(ignore_permissions=True)
	if has_lr and document.lr_date:
		# the default is applied again while inserting, so clear what was stored
		frappe.db.set_value(document.doctype, document.name, "lr_date", None, update_modified=False)
		document.lr_date = None
	return document


def _build_one(voucher: dict, company: str, cache: dict, new_items: list, new_batches: list):
	"""The draft for one voucher (inserted, not submitted), or None with the reason it moves nothing."""
	vtype = voucher["type"]
	if vtype in MONEY_TYPES:
		from dms_erp.warehouse.tally_money import build_money_draft

		return build_money_draft(voucher, company)
	if vtype in RETURN_TYPES:
		return _build_return(voucher, company, cache)
	rows = voucher.get("rows") or []
	# A sale or purchase with no stock lines is a service or expense bill (mobile maintenance, tile
	# cutting): as the migration made them, one TALLY-LEDGER-LINE line per ledger, no stock moves.
	ledger_only = not rows and vtype in PURCHASE_TYPES + ("Sales",)
	if not rows and not ledger_only:
		raise TallyVoucherError(_("The voucher has no stock lines."))
	purchase = vtype in PURCHASE_TYPES
	posting = getdate(str(voucher.get("date")))
	party_name = _clean(voucher.get("party"))
	common = {"company": company, "posting_date": posting, "set_posting_time": 1, "currency": frappe.get_cached_value("Company", company, "default_currency")}

	if purchase:
		doctype, party_type = "Purchase Invoice", "Supplier"
	elif vtype == "Sales":
		doctype, party_type = "Sales Invoice", "Customer"
	else:
		doctype, party_type = "Delivery Note", "Customer"
	party = _party(party_type, party_name)
	if not party:
		raise TallyVoucherError(_("{0} not in the ERP: {1}").format(party_type, party_name or "(blank)"))

	if ledger_only:
		party_amount = next((flt(x.get("amount")) for x in voucher.get("ledgers") or [] if x.get("party")), 0)
		lines = _ledger_items(voucher, company, party_amount, purchase, returned=False)
		if not lines:
			raise TallyVoucherError(_("The voucher has no lines to book."))
	else:
		lines = [_line(r, voucher, company, cache, purchase, new_items, new_batches) for r in rows]
	doc = {"doctype": doctype, **common, **_document_defaults(doctype, party_type, party, vtype), **_tally_fields(voucher, doctype), "items": lines}

	if purchase:
		doc.update(
			{
				"supplier": party,
				"bill_no": _clean(voucher.get("reference")) or voucher["number"],
				"bill_date": _reference_date(voucher, posting),
				"update_stock": 0 if ledger_only else 1,
				"supplier_address": _address("Supplier", party, "Billing"),
				"credit_to": _party_account("Supplier", party, company),
				"taxes": _taxes(voucher, company, True, only_tax=ledger_only),
			}
		)
	elif vtype == "Sales":
		tracked = [bool(_clean(r.get("tracking")).lower() not in NO_TRACKING) for r in rows]
		if any(tracked) and not all(tracked):
			raise TallyVoucherError(_("Some lines are against a Delivery Note and some are not; check this one in Tally."))
		doc.update(
			{
				"customer": party,
				"due_date": posting,
				# A sale raised against a delivery note moves no stock itself: the delivery note did.
				"update_stock": 0 if all(tracked) else 1,
				"customer_address": _address("Customer", party, "Billing"),
				"debit_to": _party_account("Customer", party, company),
				"taxes": _taxes(voucher, company, False, only_tax=ledger_only),
			}
		)
		if frappe.db.exists("Price List", "Standard Selling"):
			doc["selling_price_list"] = "Standard Selling"
	else:
		doc.update({"customer": party, "customer_address": _address("Customer", party, "Billing")})

	remarks = f"Tally {vtype} {voucher['number']}. Draft from the Tally voucher import; check, then submit."
	doc["remarks" if doctype != "Delivery Note" else "instructions"] = remarks
	return _insert(doc)


def _reference_date(voucher: dict, fallback):
	"""The supplier's own invoice date (Tally's reference date), which is the bill date; the voucher date
	only when Tally has none."""
	text = re.sub(r"\D", "", str(voucher.get("reference_date") or ""))
	return getdate(f"{text[:4]}-{text[4:6]}-{text[6:8]}") if len(text) == 8 else fallback


def _ledger_line_item() -> str:
	"""The non-stock Item the migration used for a note that is only ledger lines (a rate difference,
	a damages claim): made once, with no stock and no HSN. ERPNext refuses an item on a sales or
	purchase document unless it is flagged for it, and the migration's copy (which bypassed that check)
	is not, so both flags are set on it: it is a bookkeeping line, with no other use to protect."""
	if not frappe.db.exists("Item", LEDGER_LINE_ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": LEDGER_LINE_ITEM,
				"item_name": LEDGER_LINE_ITEM,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"description": "A line of a Tally note that is only ledgers (rate difference, damages): no stock.",
			}
		).insert(ignore_permissions=True)
	flags = frappe.db.get_value("Item", LEDGER_LINE_ITEM, ["is_sales_item", "is_purchase_item"])
	if not all(flags):
		frappe.db.set_value("Item", LEDGER_LINE_ITEM, {"is_sales_item": 1, "is_purchase_item": 1})
	return LEDGER_LINE_ITEM


def _value_only_items(rows: list[dict], company: str, purchase: bool) -> list[dict]:
	"""Ledger lines for stock lines that carry an amount and no quantity (see _build_return)."""
	if not rows:
		return []
	item = _ledger_line_item()
	out = []
	for row in rows:
		ledger = _clean(row.get("account"))
		account = _posting_account(ledger, company) if ledger else None
		if not account:
			raise TallyVoucherError(_("Account not in the ERP: {0}").format(ledger or "(blank)"))
		out.append(
			{
				"item_code": item,
				"item_name": item,
				"description": f"{_clean(row.get('item'))} (value only)",
				"qty": -1,
				"uom": "Nos",
				"conversion_factor": 1,
				"rate": abs(flt(row.get("amount"))),
				"tally_qty_text": "0",
				"tally_qty_value": 0,
				**_row_tally_fields(row),
				"expense_account" if purchase else "income_account": account,
			}
		)
	return out


def _party_account(party_type: str, party: str, company: str) -> str:
	"""The party's own receivable / payable account: a party the migration made as an alias has one of
	its own ("Tally Alias Customers"), and posting to the company default instead would be wrong."""
	from erpnext.accounts.party import get_party_account

	return get_party_account(party_type, party, company)


def _ledger_items(voucher: dict, company: str, party_amount: float, purchase: bool, returned: bool = True) -> list[dict]:
	"""One TALLY-LEDGER-LINE per non-tax, non-party ledger of a note with no stock lines. A ledger on the
	side Tally normally puts them (opposite the party's) is returned (negative quantity); one on the
	party's own side reduces the return (positive)."""
	item = _ledger_line_item()
	out = []
	for ledger in voucher.get("ledgers") or []:
		amount = flt(ledger.get("amount"))
		if ledger.get("party") or not amount:
			continue
		name = _clean(ledger.get("ledger"))
		account = _posting_account(name, company)
		if not account:
			raise TallyVoucherError(_("Account not in the ERP: {0}").format(name))
		if frappe.db.get_value("Account", account, "account_type") in TAX_ACCOUNT_TYPES:
			continue
		out.append(
			{
				"item_code": item,
				"item_name": item,
				"description": name,
				# opposite the party's side is the normal side for a line; a return flips every sign
				"qty": (1 if (amount > 0) != (party_amount > 0) else -1) * (-1 if returned else 1),
				"uom": "Nos",
				"conversion_factor": 1,
				"rate": abs(amount),
				"expense_account" if purchase else "income_account": account,
			}
		)
	return out


def _build_return(voucher: dict, company: str, cache: dict):
	"""A Tally credit note -> Sales Invoice return, debit note -> Purchase Invoice return (draft).

	Shaped like the migrated ones (checked on this year's 147): quantity and amount negative with a
	positive rate; with stock lines it moves stock (a credit note brings it back in, a debit note takes
	it out) unless the lines carry a delivery note's tracking number, as on a sale; with only ledger
	lines (rate difference, damages, shortage) it is the TALLY-LEDGER-LINE item, one line per ledger,
	no stock. Tally's Tax and Round Off ledgers are the tax rows. It is not linked to the invoice it
	reverses (the migration did not either): Tally's reference is kept in tally_reference."""
	vtype = voucher["type"]
	credit = vtype in CREDIT_TYPES
	doctype, party_type = ("Sales Invoice", "Customer") if credit else ("Purchase Invoice", "Supplier")
	posting = getdate(str(voucher.get("date")))
	party_name = _clean(voucher.get("party"))
	party = _party(party_type, party_name)
	if not party:
		raise TallyVoucherError(_("{0} not in the ERP: {1}").format(party_type, party_name or "(blank)"))
	all_rows = voucher.get("rows") or []
	# A stock line with an amount but no quantity is a value adjustment on that item (a price corrected by
	# credit note): the migration booked it as a ledger line described "<item> (value only)", no stock.
	rows = [r for r in all_rows if _clean(r.get("quantity"))]
	value_rows = [r for r in all_rows if not _clean(r.get("quantity"))]
	party_amount = next((flt(x.get("amount")) for x in voucher.get("ledgers") or [] if x.get("party")), 0)
	update_stock = 0
	items = []
	if rows:
		tracked = [_clean(r.get("tracking")).lower() not in NO_TRACKING for r in rows]
		if any(tracked) and not all(tracked):
			raise TallyVoucherError(_("Some lines are against a delivery note and some are not; check this one in Tally."))
		update_stock = 0 if all(tracked) else 1
		items = [_line(r, voucher, company, cache, False, [], [], sign=-1) for r in rows]
	items += _value_only_items(value_rows, company, not credit)
	if not all_rows:
		items = _ledger_items(voucher, company, party_amount, not credit)
	if not items:
		raise TallyVoucherError(_("The voucher has no lines to return."))
	account = _party_account(party_type, party, company)
	doc = {
		"doctype": doctype,
		"company": company,
		"posting_date": posting,
		"set_posting_time": 1,
		"currency": frappe.get_cached_value("Company", company, "default_currency"),
		**_document_defaults(doctype, party_type, party, vtype),
		**_tally_fields(voucher, doctype),
		"is_return": 1,
		"update_stock": update_stock,
		"items": items,
		"taxes": _taxes(voucher, company, not credit, only_tax=not all_rows),
	}
	if credit:
		doc.update({"customer": party, "due_date": posting, "debit_to": account, "customer_address": _address("Customer", party, "Billing")})
		if frappe.db.exists("Price List", "Standard Selling"):
			doc["selling_price_list"] = "Standard Selling"
	else:
		doc.update(
			{
				"supplier": party,
				"bill_no": _clean(voucher.get("reference")) or voucher["number"],
				"bill_date": _reference_date(voucher, posting),
				"credit_to": account,
				"supplier_address": _address("Supplier", party, "Billing"),
			}
		)
	narration = _clean(voucher.get("narration"))
	note = f"Tally {vtype} {voucher['number']}. Draft from the Tally voucher import; check, then submit."
	doc["remarks"] = f"{narration} ({note})" if narration else note
	return _insert(doc)


def _tally_total(voucher: dict):
	"""What Tally says the voucher came to: the party ledger's amount."""
	for ledger in voucher.get("ledgers") or []:
		if ledger.get("party"):
			return abs(flt(ledger.get("amount")))
	return None


def _build_drafts(vouchers: list[dict], company: str) -> dict:
	cache: dict = {}
	created, unplaced, ignored = [], [], {}
	new_items: list = []
	new_batches: list = []
	seen: set[str] = set()
	held = known_guids([v.get("guid") for v in vouchers])
	already = 0

	for voucher in vouchers:
		guid, vtype, number = voucher.get("guid"), voucher.get("type"), voucher.get("number")
		label = {"voucher": number, "type": vtype}
		if not guid or guid in seen:
			continue
		seen.add(guid)
		if guid in held:
			already += 1
			continue
		if voucher.get("cancelled") or voucher.get("optional"):
			ignored["Cancelled or optional"] = ignored.get("Cancelled or optional", 0) + 1
			continue
		if vtype not in HANDLED_TYPES:
			why = f"{vtype} vouchers are not imported yet."
			ignored[why] = ignored.get(why, 0) + 1
			continue
		savepoint = f"tv_{len(created) + len(unplaced)}"
		frappe.db.savepoint(savepoint)
		items_before, batches_before = len(new_items), len(new_batches)
		try:
			doc = _build_one(voucher, company, cache, new_items, new_batches)
			if doc.doctype in ("Payment Entry", "Journal Entry"):
				created.append({**label, "doctype": doc.doctype, "name": doc.name, "amount": flt(doc.get("paid_amount") or doc.get("total_debit"))})
				continue
			entry = {**label, "doctype": doc.doctype, "name": doc.name, "rows": len(doc.items), "stockUpdate": bool(doc.get("update_stock", 1))}
			total = _tally_total(voucher)
			if total is not None and doc.doctype != "Delivery Note":
				entry.update({"tallyTotal": total, "erpTotal": abs(flt(doc.grand_total)), "difference": round(abs(flt(doc.grand_total)) - total, 2)})
			created.append(entry)
		except (TallyVoucherError, frappe.ValidationError, frappe.MandatoryError) as e:
			frappe.db.rollback(save_point=savepoint)
			# Anything this voucher created (an item, a batch) went with the rollback; so does its report.
			del new_items[items_before:]
			del new_batches[batches_before:]
			unplaced.append({**label, "reason": _clean(frappe.safe_decode(str(e)))[:300]})
		except Exception:  # noqa: BLE001 -- by design: one bad voucher must not stop the rest; it is logged and reported
			frappe.db.rollback(save_point=savepoint)
			del new_items[items_before:]
			del new_batches[batches_before:]
			frappe.log_error(title=f"Tally voucher import failed ({number})")
			unplaced.append({**label, "reason": "Unexpected error; see the Error Log."})

	frappe.logger("tally_vouchers").info(
		f"Tally voucher import: {len(created)} drafts, {already} already in the ERP, {len(unplaced)} not placed, ignored {ignored}"
	)
	return {
		"created": created,
		"newItems": new_items,
		"newBatches": new_batches,
		"alreadyInErp": already,
		"ignored": ignored,
		"unplaced": unplaced[:200],
		"unplacedTotal": len(unplaced),
	}
