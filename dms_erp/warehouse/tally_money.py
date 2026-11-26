"""Tally receipts, payments and contra -> draft Payment Entry / Journal Entry.

Same two-document shape the one-off migration used, so a voucher that arrives now looks like the ones
already in the books:

- A receipt or payment between ONE bank/cash ledger and ONE dealer or supplier -> Payment Entry, tied
  to the invoices Tally says it settles ("Agst Ref") when those are submitted in the ERP.
- Everything else (an expense paid from the bank, a contra, a receipt from a ledger that is not a
  customer, a voucher with several counter ledgers, a sign that does not fit its type) -> Journal Entry,
  which carries any ledger pair exactly as Tally booked it.

Why a Journal Entry rather than a refusal for the odd ones: it is always a faithful copy of the debits
and credits, and the accountant reviews every draft before it is submitted anyway. What is NOT done is
inventing a ledger: a Tally ledger with no matching Account stops that voucher and says which.

Tally's sign: a negative amount is a debit, positive a credit (receipt: bank -X, party +X).
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from dms_erp.warehouse.tally_vouchers import (
	CONTRA_TYPES,
	RECEIPT_TYPES,
	TallyVoucherError,
	_clean,
	_party,
	_posting_account,
	_tally_fields,
)

# Tally's "no allocation" names for a settled amount: only a named invoice is a reference.
AGAINST = "agst ref"


def _date(text, fallback):
	text = _clean(text)
	return getdate(f"{text[:4]}-{text[4:6]}-{text[6:8]}") if len(text) == 8 and text.isdigit() else fallback


def _lines(voucher: dict, company: str) -> list[dict]:
	"""Each ledger line with its ERP Account, debit/credit and what kind of account it is."""
	out = []
	for line in voucher.get("ledgers") or []:
		amount = flt(line.get("amount"))
		if not amount:
			continue
		ledger = _clean(line.get("ledger"))
		account = _posting_account(ledger, company)
		kind = frappe.db.get_value("Account", account, "account_type") if account else None
		party = None
		# A ledger with its own Account is an account, not a party: the migration also made placeholder
		# Customers ("Tally Alias Customers") named after bank, cash and expense ledgers, and taking
		# "Cash" for a dealer would book a cash receipt to the wrong side of the ledger.
		if not account or kind in ("Receivable", "Payable"):
			for party_type in ("Customer", "Supplier"):
				found = _party(party_type, ledger)
				if found:
					party = (party_type, found)
					break
		if not account and not party:
			raise TallyVoucherError(_("Ledger not in the ERP chart of accounts: {0}").format(ledger or "(blank)"))
		out.append(
			{
				"ledger": ledger,
				"account": account,
				"party": party,
				"kind": kind,
				"debit": abs(amount) if amount < 0 else 0.0,
				"credit": amount if amount > 0 else 0.0,
				"bills": line.get("bills") or [],
				"instrument": _clean(line.get("instrument")),
				"instrument_date": line.get("instrument_date"),
				"unique_ref": _clean(line.get("unique_ref")),
			}
		)
	if len(out) < 2:
		raise TallyVoucherError(_("The voucher has fewer than two ledger lines."))
	return out


def build_money_draft(voucher: dict, company: str):
	"""The draft (inserted, not submitted) for one money voucher."""
	posting = getdate(str(voucher.get("date")))
	lines = _lines(voucher, company)
	vtype = voucher["type"]
	# A party ledger has no Account of its own in this chart: it is the Customer / Supplier, whose
	# account is the company's receivable / payable. So "money" is decided on real accounts only.
	money = [x for x in lines if x["kind"] in ("Bank", "Cash") and not x["party"]]
	others = [x for x in lines if x not in money]

	if vtype not in CONTRA_TYPES and len(money) == 1 and len(others) == 1 and others[0]["party"]:
		doc = _payment_entry(voucher, company, posting, money[0], others[0], receive=vtype in RECEIPT_TYPES)
		if doc:
			return doc
	return _journal_entry(voucher, company, posting, lines)


def _find_bill(party_type: str, party: str, name: str, company: str):
	"""(doctype, docname, outstanding) of the submitted document Tally's bill name stands for, or None.
	`tally_bill_no` holds Tally's bill name (with the year suffix) on migrated invoices AND on a
	transporter's freight bill (Goods Consignment Note), which the migration kept as a Journal Entry and
	settled with a Payment Entry. A new Tally sale / purchase imported here keeps it in tally_voucher_no
	and bill_no, so those are tried too. More than one match is "not found": never guess between two."""
	doctype = "Sales Invoice" if party_type == "Customer" else "Purchase Invoice"
	by = [["name", "=", name], ["tally_voucher_no", "=", name], ["tally_bill_no", "=", name]]
	if doctype == "Purchase Invoice":
		by.append(["bill_no", "=", name])
	found = frappe.get_all(
		doctype,
		filters={"customer" if party_type == "Customer" else "supplier": party, "company": company, "docstatus": 1, "is_return": 0},
		or_filters=by,
		fields=["name", "outstanding_amount"],
		limit=2,
	)
	if len(found) == 1:
		return doctype, found[0].name, flt(found[0].outstanding_amount)
	if found or party_type != "Supplier":
		return None
	# a freight bill: a Journal Entry that owes this supplier
	entries = [
		je
		for je in frappe.get_all(
			"Journal Entry",
			filters={"company": company, "docstatus": 1},
			or_filters=[["tally_bill_no", "=", name], ["tally_voucher_no", "=", name]],
			pluck="name",
			limit=3,
		)
		if frappe.db.exists("Journal Entry Account", {"parent": je, "party_type": "Supplier", "party": party})
	]
	if len(entries) != 1:
		return None
	from erpnext.accounts.doctype.payment_entry.payment_entry import (
		get_reference_details,
	)

	account_currency = frappe.get_cached_value("Company", company, "default_currency")
	details = get_reference_details("Journal Entry", entries[0], account_currency, party_type, party)
	return "Journal Entry", entries[0], flt(details.get("outstanding_amount"))


def _open_invoices(party_type: str, party: str, bills: list[dict], company: str):
	"""([references], [notes]): what this voucher settles that the ERP can take a payment against
	(submitted, still owing), and a note on each bill it cannot place."""
	refs, notes = [], []
	for bill in bills:
		name = _clean(bill.get("name"))
		if _clean(bill.get("type")).lower() != AGAINST:
			continue
		amount = abs(flt(bill.get("amount")))
		hit = _find_bill(party_type, party, name, company)
		if not hit:
			notes.append(_("Bill {0}: not found as a submitted document, so it is not allocated; allocate it by hand.").format(name))
			continue
		doctype, docname, outstanding = hit
		allocated = min(amount, outstanding)
		if allocated <= 0:
			notes.append(_("Bill {0} ({1}): nothing outstanding, so it is not allocated.").format(name, docname))
			continue
		refs.append({"reference_doctype": doctype, "reference_name": docname, "allocated_amount": allocated})
		if allocated < amount:
			notes.append(_("Bill {0} ({1}): only {2} was outstanding of the {3} Tally allocated.").format(name, docname, allocated, amount))
	return refs, notes


def _payment_entry(voucher, company, posting, money, counter, receive: bool):
	"""None when the voucher does not fit a Payment Entry (wrong direction): the caller falls back to a Journal Entry."""
	party_type, party = counter["party"]
	amount = money["debit"] if receive else money["credit"]
	other_side = money["credit"] if receive else money["debit"]
	if not amount or other_side:
		return None
	if abs((counter["credit"] if receive else counter["debit"]) - amount) > 0.005:
		return None
	from erpnext.accounts.party import get_party_account

	party_account = get_party_account(party_type, party, company)
	instrument = money["instrument"] or _clean(voucher["number"])
	bills = counter["bills"] + money["bills"]
	# The party line's own sign is the direction every bill should run in. A bill against it means Tally
	# netted another document off in this same voucher (a receipt that also cancels an earlier advance),
	# and bills that add up to more than the money moved cannot all be allocated: either way nothing is
	# guessed, the entry is left unallocated for a person.
	direction = 1 if counter["credit"] else -1
	against = [b for b in bills if _clean(b.get("type")).lower() == AGAINST]
	if any(flt(b.get("amount")) * direction < 0 for b in against) or sum(abs(flt(b.get("amount"))) for b in against) > amount + 0.005:
		refs, notes = [], [_("Tally netted other documents in this voucher (or allocated more than was paid), so nothing is allocated; allocate it by hand.")]
	else:
		refs, notes = _open_invoices(party_type, party, bills, company)
	narration = _clean(voucher.get("narration"))
	remarks = " ".join(
		[f"Tally {voucher['type']} {voucher['number']}.", narration, " ".join(notes), "Draft from the Tally voucher import; check, then submit."]
	).replace("  ", " ")
	doc = {
		"doctype": "Payment Entry",
		"company": company,
		"payment_type": "Receive" if receive else "Pay",
		"posting_date": posting,
		"party_type": party_type,
		"party": party,
		"paid_from": party_account if receive else money["account"],
		"paid_to": money["account"] if receive else party_account,
		"paid_amount": amount,
		"received_amount": amount,
		"reference_no": instrument,
		"reference_date": _date(money["instrument_date"], posting),
		"references": refs,
		# Without custom_remarks ERPNext overwrites remarks with "Amount X received from Y".
		"custom_remarks": 1,
		"remarks": remarks,
		**_tally_fields(voucher, "Payment Entry"),
	}
	document = frappe.get_doc(doc)
	document.insert(ignore_permissions=True)
	return document


def _cheque(voucher, first, posting, voucher_type: str) -> dict:
	"""The journal's cheque number and date, as the migrated ones carry them (checked on every kind):
	a cash entry has none; a bank entry (a bank receipt or payment that is not a party payment) has the
	voucher number and the voucher date -- the transfer's own number is in the narration; a contra has the
	transfer's instrument number, or Tally's unique reference for it, and the instrument's date."""
	if voucher_type == "Cash Entry":
		return {}
	if voucher_type == "Contra Entry" and first:
		return {"cheque_no": first["instrument"] or first["unique_ref"] or _clean(voucher["number"]), "cheque_date": _date(first["instrument_date"], posting)}
	return {"cheque_no": _clean(voucher["number"]), "cheque_date": posting}


def _journal_entry(voucher, company, posting, lines):
	vtype = voucher["type"]
	bank = next((x for x in lines if x["kind"] == "Bank" and not x["party"]), None)
	voucher_type = "Contra Entry" if vtype in CONTRA_TYPES else ("Bank Entry" if bank else "Cash Entry")
	first = next((x for x in lines if x["instrument"] or x["unique_ref"] or x["instrument_date"]), None)
	rows = []
	for x in lines:
		row = {"debit_in_account_currency": x["debit"], "credit_in_account_currency": x["credit"]}
		if x["party"]:
			party_type, party = x["party"]
			row.update(
				{
					"account": _party_account(party_type, party, company),
					"party_type": party_type,
					"party": party,
				}
			)
		else:
			row["account"] = x["account"]
		rows.append(row)
	narration = _clean(voucher.get("narration"))
	doc = {
		"doctype": "Journal Entry",
		"company": company,
		"voucher_type": voucher_type,
		"posting_date": posting,
		**_cheque(voucher, first, posting, voucher_type),
		"user_remark": f"{narration} (Tally {vtype} {voucher['number']}; draft from the Tally voucher import, check, then submit.)".strip(),
		"accounts": rows,
		**_tally_fields(voucher, "Journal Entry"),
	}
	document = frappe.get_doc(doc)
	document.insert(ignore_permissions=True)
	return document


def _party_account(party_type: str, party: str, company: str) -> str:
	from erpnext.accounts.party import get_party_account

	return get_party_account(party_type, party, company)
