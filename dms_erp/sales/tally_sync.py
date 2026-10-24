"""ERP -> Tally sync of submitted Sales Invoices (client request, Oct 2026 -- not in the
BRD, which only scopes a one-time migration from "Tally Prime Edition 7.1").

Tally is still Pacific's live system of record and runs on a PC in their office; this
ERP runs in the cloud. Neither can call the other: the cloud can't reach an office
LAN, and Tally has no outbound hook -- it only answers requests on its own XML port
(9000). So the sync is pull-based: a small agent on the Tally PC
(tools/tally_sync_agent) asks this module for invoices waiting to go, posts each one
to Tally as a Sales voucher, and reports the outcome back. Everything that needs ERP
knowledge -- ledger names, godowns, batch labels, Tally's "N Box M peices" quantity
format -- is decided here, so the agent stays a dumb courier and the mapping is
covered by the FrappeTestCase suite instead of living untested on a client PC.

Opt-in per invoice ("Sync to Tally"), submitted only: a draft is never sent, and an
invoice that already carries a Tally voucher number or GUID came FROM Tally
(migration, or the Tally -> ERP direction later) and is never sent back, which is what
stops the two directions from looping. The ERP invoice number is used as the Tally
voucher number, so one ERP invoice can only ever be one Tally voucher -- the agent
checks Tally for that number before creating anything. E-invoice/IRN stays in the ERP:
the voucher carries no IRN, so Tally never generates a second one.

Not built yet (deliberately, until the one-way flow is proven): cancelling or amending
in the ERP after a voucher reached Tally only records a note -- the Tally voucher has to
be cancelled there by hand -- and credit notes (is_return) are skipped.
"""

from xml.sax.saxutils import escape

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime

SYNC_ROLES = ("System Manager", "Accounts Manager")

# Off until the sync is fully completed and signed off: the Sales Invoice hooks are
# commented out in hooks.py and the custom fields aren't created on migrate, and the
# two agent endpoints below refuse while this is False.
TALLY_SYNC_ENABLED = False


def _assert_enabled():
	if not TALLY_SYNC_ENABLED:
		frappe.throw(_("ERP -> Tally sync is not enabled on this site yet."), frappe.ValidationError)

STATUS_PENDING = "Pending"
STATUS_SYNCED = "Synced"
STATUS_FAILED = "Failed"
STATUS_SKIPPED = "Skipped"

DEFAULT_SALES_VOUCHER_TYPE = "Sales"
# Units Tally keeps as the "pieces" half of a compound "Box of N <unit>" -- the
# migration turned each of those into Box + a UOM Conversion Detail row for this unit.
PIECE_UOMS = ("peices", "pcs", "sheets", "Nos")


class TallyMappingError(frappe.ValidationError):
	pass


# ---------------------------------------------------------------- document events


SYNC_FIELDS = ("tally_sync_status", "tally_synced_voucher_no", "tally_synced_on", "tally_sync_error")


def on_submit(doc, method=None):
	_queue_if_requested(doc, _sync_state(doc))


def on_update_after_submit(doc, method=None):
	"""Ticking "Sync to Tally" on an already-submitted invoice queues it too, and so
	does re-ticking a Failed one -- that's the retry path, no Desk button needed."""
	_queue_if_requested(doc, _sync_state(doc))


def on_cancel(doc, method=None):
	state = _sync_state(doc)
	if state.get("tally_sync_status") == STATUS_PENDING:
		_set(doc.name, tally_sync_status=STATUS_SKIPPED, tally_sync_error=_("Cancelled in the ERP before it reached Tally."))
	elif state.get("tally_sync_status") == STATUS_SYNCED:
		_set(
			doc.name,
			**{**state, "tally_sync_error": _("Cancelled in the ERP after syncing: cancel Tally voucher {0} in Tally by hand.").format(
				state.get("tally_synced_voucher_no") or doc.name
			)},
		)


def _sync_state(doc) -> dict:
	"""The sync fields as they really stand. Only this module and the agent write them,
	always straight to the database, so whatever copy of the invoice a user saves or
	cancels from was loaded before and still holds old values -- and Frappe writes that
	whole copy back before on_update_after_submit / on_cancel run, silently undoing a
	Synced (voucher number included) or Pending mark. The copy Frappe loaded just
	before saving is the true state; this puts it back when the save overwrote it."""
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if not before:
		return {f: doc.get(f) for f in SYNC_FIELDS}
	state = {f: before.get(f) for f in SYNC_FIELDS}
	if any(doc.get(f) != state[f] for f in SYNC_FIELDS):
		_set(doc.name, **state)
		for f, v in state.items():
			doc.set(f, v)
	return state


def _queue_if_requested(doc, state):
	if doc.docstatus != 1 or not cint(doc.get("sync_to_tally")):
		return
	if state.get("tally_sync_status") in (STATUS_PENDING, STATUS_SYNCED):
		return
	if _came_from_tally(doc):
		_set(doc.name, tally_sync_status=STATUS_SKIPPED, tally_sync_error=_("This invoice came from Tally, so it is not sent back."))
		return
	if cint(doc.get("is_return")):
		_set(doc.name, tally_sync_status=STATUS_SKIPPED, tally_sync_error=_("Credit notes are not synced to Tally yet."))
		return
	_set(doc.name, tally_sync_status=STATUS_PENDING, tally_sync_error=None)


def _came_from_tally(doc) -> bool:
	return bool(doc.get("tally_voucher_no") or doc.get("tally_guid"))


def _set(name, **values):
	frappe.db.set_value("Sales Invoice", name, values, update_modified=False)


# ------------------------------------------------------------------ agent API


@frappe.whitelist(methods=["POST"])
def get_pending_invoices(limit: int = 20):
	"""Invoices waiting for Tally, each already converted to a Tally Sales voucher.
	One that can't be converted (a ledger/batch the mapping can't resolve) is marked
	Failed with the reason instead of blocking the rest of the queue -- which is a
	write, hence POST: Frappe rolls back whatever a GET request writes."""
	_assert_enabled()
	frappe.only_for(SYNC_ROLES)
	names = frappe.get_all(
		"Sales Invoice",
		filters={"docstatus": 1, "sync_to_tally": 1, "tally_sync_status": STATUS_PENDING},
		pluck="name",
		order_by="posting_date asc, creation asc",
		limit=min(cint(limit) or 20, 100),
	)
	out = []
	for name in names:
		doc = frappe.get_doc("Sales Invoice", name)
		try:
			out.append(
				{
					"invoice": name,
					"voucher_number": name,
					# What the agent matches Tally on: Pacific's Sales voucher type numbers
					# itself, so the ERP invoice number lives in the voucher's Reference.
					"reference": name,
					"voucher_type": _sales_voucher_type(),
					"date": getdate(doc.posting_date).strftime("%Y%m%d"),
					"voucher_xml": build_voucher_xml(doc),
				}
			)
		except TallyMappingError as e:
			_set(name, tally_sync_status=STATUS_FAILED, tally_sync_error=str(e))
	return out


@frappe.whitelist(methods=["POST"])
def report_sync_result(invoice: str, ok, tally_voucher_no: str | None = None, error: str | None = None):
	_assert_enabled()
	frappe.only_for(SYNC_ROLES)
	if frappe.db.get_value("Sales Invoice", invoice, "tally_sync_status") != STATUS_PENDING:
		# Already reported (an agent retry after a network blip) -- first answer wins.
		return {"updated": False}
	if cint(ok):
		_set(
			invoice,
			tally_sync_status=STATUS_SYNCED,
			tally_sync_error=None,
			tally_synced_voucher_no=tally_voucher_no or invoice,
			tally_synced_on=now_datetime(),
		)
	else:
		_set(invoice, tally_sync_status=STATUS_FAILED, tally_sync_error=(error or _("Tally rejected the voucher."))[:1000])
	return {"updated": True}


def _sales_voucher_type() -> str:
	return frappe.conf.get("dms_erp_tally_sales_voucher_type") or DEFAULT_SALES_VOUCHER_TYPE


# --------------------------------------------------------------- ERP -> Tally mapping


def tally_ledger_name(account: str, company: str) -> str:
	"""ERP account "09% Output CGST - PI" is Tally ledger "09% Output CGST": the
	migration created every account from a Tally ledger plus the company suffix."""
	abbr = frappe.get_cached_value("Company", company, "abbr")
	suffix = f" - {abbr}"
	return account[: -len(suffix)] if abbr and account.endswith(suffix) else account


def tally_godown_name(warehouse: str) -> str:
	"""Warehouse names are Tally godown names; Tally lets a parent godown hold stock
	itself, ERPNext doesn't, so the migration gave each such parent a "<name> (Main)"
	leaf -- that leaf IS the Tally parent godown."""
	name = frappe.get_cached_value("Warehouse", warehouse, "warehouse_name") or warehouse
	return name.removesuffix(" (Main)")


def tally_batch_name(batch: str, item_code: str) -> str:
	"""ERP batch names are unique system-wide, Tally's only per item, so a label two
	items share was saved as "<label> - <item>" -- Tally knows it as just <label>."""
	if frappe.get_meta("Batch").has_field("tally_batch_name"):
		label = frappe.get_cached_value("Batch", batch, "tally_batch_name")
		if label:
			return label
	return batch.removesuffix(f" - {item_code}")


def piece_unit(item_code: str, stock_uom: str):
	"""(unit name, pieces per box) when the item is a Tally compound "Box of N <unit>",
	else None."""
	if stock_uom != "Box":
		return None
	for uom, factor in frappe.db.sql(
		"""select uom, conversion_factor from `tabUOM Conversion Detail`
		where parent=%s and uom in %s and conversion_factor > 0 and conversion_factor < 1""",
		(item_code, PIECE_UOMS),
	):
		return uom, round(1 / flt(factor))
	return None


def tally_qty(qty: float, stock_uom: str, pieces=None) -> str:
	"""Tally's own quantity text: " 3 Box 1 peices" for a compound unit (stored as
	3.25 Box in the ERP), " 12 Slabs" otherwise."""
	qty = flt(qty, 6)
	if not pieces:
		return f" {qty:g} {stock_uom}"
	unit, per_box = pieces
	boxes = int(qty)
	loose = (qty - boxes) * per_box
	if abs(loose - round(loose)) > 0.01:
		raise TallyMappingError(
			_("{0} {1} is not a whole number of {2} ({3} per box).").format(qty, stock_uom, unit, per_box)
		)
	loose = round(loose)
	if loose >= per_box:
		boxes, loose = boxes + 1, loose - per_box
	return f" {boxes} Box {loose} {unit}"


def _bundle_entries(bundle, fallback_warehouse):
	return [
		(wh or fallback_warehouse, batch, abs(flt(qty)))
		for wh, batch, qty in frappe.db.sql(
			"""select warehouse, batch_no, sum(qty) from `tabSerial and Batch Entry`
			where parent=%s and ifnull(batch_no, '') != '' group by warehouse, batch_no order by min(idx)""",
			bundle,
		)
	]


def _row_allocations(row):
	"""[(warehouse, batch, qty)] -- where this invoice line's stock physically came
	from. An invoice that moved stock itself carries it (Serial and Batch Bundle, or
	batch_no); one billed from a Delivery Note has it on the DN line; one the staff app
	bills straight from a Sales Order (order_api.create_sales_invoice) has neither, so
	the order's Pick Tasks -- one per line, with the batch and bay actually picked --
	are the record of it."""
	wh = row.get("warehouse")
	if row.get("serial_and_batch_bundle"):
		return _bundle_entries(row.serial_and_batch_bundle, wh)
	if row.get("batch_no"):
		return [(wh, row.batch_no, flt(row.stock_qty))]
	if row.get("dn_detail"):
		dn = frappe.db.get_value(
			"Delivery Note Item", row.dn_detail, ["warehouse", "batch_no", "serial_and_batch_bundle", "stock_qty"], as_dict=True
		)
		if dn and dn.serial_and_batch_bundle:
			return _bundle_entries(dn.serial_and_batch_bundle, dn.warehouse or wh)
		if dn and dn.batch_no:
			return [(dn.warehouse or wh, dn.batch_no, flt(row.stock_qty))]
	if row.get("sales_order"):
		filters = {"sales_order": row.sales_order, "item": row.item_code, "batch_no": ["is", "set"]}
		if row.get("so_detail"):
			# the order line itself -- an order can carry the same item on two lines
			filters["sales_order_item"] = row.so_detail
		tasks = frappe.get_all(
			"Pick Task",
			filters=filters,
			fields=["suggested_bay", "batch_no", "qty", "allocated_qty"],
			order_by="creation asc",
		)
		return [(t.suggested_bay or wh, t.batch_no, flt(t.allocated_qty) or flt(t.qty)) for t in tasks]
	return []


def build_voucher_xml(doc) -> str:
	"""One Tally Sales voucher (Invoice view, items with godown/batch, ledgers) for a
	submitted ERP Sales Invoice -- the same shape Tally itself exports for Pacific's own
	sales vouchers. Amount signs follow Tally's import convention: debit negative,
	credit positive."""
	x = escape
	lines = []
	items_total = 0.0
	# An invoice-level discount is spread over the lines and each line rounded on its
	# own, so the lines can miss the invoice's net total by a paisa -- the last line
	# takes that difference, or Tally (and the balance check below) sees it unbalanced.
	line_amounts = [flt(r.base_net_amount, 2) for r in doc.items]
	net_total = flt(doc.get("base_net_total"), 2)
	drift = flt(net_total - sum(line_amounts), 2) if net_total else 0
	if line_amounts and drift and abs(drift) <= 0.05 * len(line_amounts):
		line_amounts[-1] = flt(line_amounts[-1] + drift, 2)
	for row, amount in zip(doc.items, line_amounts):
		stock_uom = row.get("stock_uom") or frappe.get_cached_value("Item", row.item_code, "stock_uom")
		stock_qty = flt(row.get("stock_qty") or row.qty)
		if not stock_qty:
			raise TallyMappingError(_("Row {0} ({1}) has no quantity.").format(row.idx, row.item_code))
		pieces = piece_unit(row.item_code, stock_uom)
		rate = amount / stock_qty
		items_total += amount
		has_batch = cint(frappe.get_cached_value("Item", row.item_code, "has_batch_no"))
		allocs = _row_allocations(row) if has_batch else [(row.warehouse, None, stock_qty)]
		if has_batch and not allocs:
			raise TallyMappingError(
				_("Row {0} ({1}): no batch found on the invoice, its Delivery Note or the order's Pick Tasks, and Tally needs one.").format(
					row.idx, row.item_code
				)
			)
		if abs(sum(q for _w, _b, q in allocs) - stock_qty) > 0.0001:
			raise TallyMappingError(
				_("Row {0} ({1}): batches add up to {2} but the invoice bills {3}.").format(
					row.idx, row.item_code, flt(sum(q for _w, _b, q in allocs), 6), stock_qty
				)
			)
		allocations = []
		remaining = amount
		for i, (warehouse, batch, bqty) in enumerate(allocs):
			if not warehouse:
				raise TallyMappingError(_("Row {0} ({1}) has no warehouse.").format(row.idx, row.item_code))
			# the last allocation takes the remainder so the batches sum to the line exactly
			bamount = remaining if i == len(allocs) - 1 else flt(rate * bqty, 2)
			remaining = flt(remaining - bamount, 2)
			allocations.append(
				"<BATCHALLOCATIONS.LIST>"
				f"<GODOWNNAME>{x(tally_godown_name(warehouse))}</GODOWNNAME>"
				+ (f"<BATCHNAME>{x(tally_batch_name(batch, row.item_code))}</BATCHNAME>" if batch else "")
				+ f"<AMOUNT>{bamount:.2f}</AMOUNT>"
				f"<ACTUALQTY>{x(tally_qty(bqty, stock_uom, pieces))}</ACTUALQTY>"
				f"<BILLEDQTY>{x(tally_qty(bqty, stock_uom, pieces))}</BILLEDQTY>"
				"</BATCHALLOCATIONS.LIST>"
			)
		lines.append(
			"<ALLINVENTORYENTRIES.LIST>"
			f"<STOCKITEMNAME>{x(row.item_code)}</STOCKITEMNAME>"
			"<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>"
			f"<RATE>{rate:.4f}/{x(stock_uom)}</RATE>"
			f"<AMOUNT>{amount:.2f}</AMOUNT>"
			f"<ACTUALQTY>{x(tally_qty(stock_qty, stock_uom, pieces))}</ACTUALQTY>"
			f"<BILLEDQTY>{x(tally_qty(stock_qty, stock_uom, pieces))}</BILLEDQTY>"
			+ "".join(allocations)
			+ "<ACCOUNTINGALLOCATIONS.LIST>"
			f"<LEDGERNAME>{x(tally_ledger_name(row.income_account, doc.company))}</LEDGERNAME>"
			"<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>"
			f"<AMOUNT>{amount:.2f}</AMOUNT>"
			"</ACCOUNTINGALLOCATIONS.LIST>"
			"</ALLINVENTORYENTRIES.LIST>"
		)

	credits = []  # (ledger, amount) -- taxes and round off, credit side
	for tax in doc.get("taxes") or []:
		amt = flt(tax.base_tax_amount_after_discount_amount, 2)
		if amt:
			credits.append((tally_ledger_name(tax.account_head, doc.company), amt))
	total = flt(doc.base_rounded_total or doc.base_grand_total, 2)
	round_off = flt(doc.get("base_rounding_adjustment"), 2)
	# "Discount on Grand Total" makes ERPNext's grand total and net + taxes differ by a
	# paisa or two; ERPNext itself books that gap to the Round Off account in its own
	# GL (e.g. 0.12 rounding + 0.01 gap = Round Off 0.13), so Tally gets the same.
	gap = flt(total - items_total - sum(a for _l, a in credits) - round_off, 2)
	if gap and abs(gap) <= 0.05:
		round_off = flt(round_off + gap, 2)
	if round_off:
		account = frappe.get_cached_value("Company", doc.company, "round_off_account")
		if not account:
			raise TallyMappingError(_("Company {0} has no Round Off account.").format(doc.company))
		credits.append((tally_ledger_name(account, doc.company), round_off))

	# Tally refuses a voucher whose debits and credits differ by even a paisa.
	if abs(flt(items_total + sum(a for _l, a in credits), 2) - total) >= 0.005:
		raise TallyMappingError(
			_("Items + taxes + round off ({0}) don't add up to the invoice total ({1}).").format(
				flt(items_total + sum(a for _l, a in credits), 2), total
			)
		)

	party = frappe.get_cached_value("Customer", doc.customer, "customer_name") or doc.customer
	ledger_entries = [
		(
			"<LEDGERENTRIES.LIST>"
			f"<LEDGERNAME>{x(party)}</LEDGERNAME>"
			"<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><ISPARTYLEDGER>Yes</ISPARTYLEDGER>"
			f"<AMOUNT>{-total:.2f}</AMOUNT>"
			"</LEDGERENTRIES.LIST>"
		)
	]
	for ledger, amt in credits:
		ledger_entries.append(
			"<LEDGERENTRIES.LIST>"
			f"<LEDGERNAME>{x(ledger)}</LEDGERNAME>"
			f"<ISDEEMEDPOSITIVE>{'Yes' if amt < 0 else 'No'}</ISDEEMEDPOSITIVE>"
			f"<AMOUNT>{amt:.2f}</AMOUNT>"
			"</LEDGERENTRIES.LIST>"
		)

	date = getdate(doc.posting_date).strftime("%Y%m%d")
	vtype = _sales_voucher_type()
	return (
		f'<VOUCHER VCHTYPE="{x(vtype)}" ACTION="Create" OBJVIEW="Invoice Voucher View">'
		f"<DATE>{date}</DATE><EFFECTIVEDATE>{date}</EFFECTIVEDATE>"
		f"<VOUCHERTYPENAME>{x(vtype)}</VOUCHERTYPENAME>"
		f"<VOUCHERNUMBER>{x(doc.name)}</VOUCHERNUMBER>"
		f"<REFERENCE>{x(doc.name)}</REFERENCE>"
		f"<PARTYLEDGERNAME>{x(party)}</PARTYLEDGERNAME>"
		f"<BASICBUYERNAME>{x(party)}</BASICBUYERNAME>"
		"<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>"
		"<ISINVOICE>Yes</ISINVOICE>"
		f"<NARRATION>{x(_('ERP Sales Invoice {0}').format(doc.name))}</NARRATION>"
		+ "".join(lines)
		+ "".join(ledger_entries)
		+ "</VOUCHER>"
	)
