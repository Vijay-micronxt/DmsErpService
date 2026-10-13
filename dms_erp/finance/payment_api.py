"""Receipts and payments read API. Money in or out of a bank/cash account is recorded
two ways in ERPNext, and the Tally migration used both: a Payment Entry (party
receipts/payments) and a Journal Entry of voucher_type "Bank Entry"/"Cash Entry"
(most bank and cash vouchers, including expenses with no party at all). Listing only
Payment Entries would hide most of them, so both are merged into one list.

A journal's direction and amount come from its bank/cash line (Account.account_type
Bank/Cash): money into that account is a receipt, money out is a payment. Its
"party" is the party on its other side, or -- for an expense voucher with no party --
the counter account (e.g. a salary or rent ledger). A journal whose bank/cash lines
net to zero is a transfer between the company's own bank/cash accounts, not a
receipt or payment, and is left out.

Restricted to Management: these include owner drawings and salary payments, not just
dealer and supplier money.
"""

import frappe
from frappe import _

from dms_erp.pagination import clamp

PAYMENT_READ_ROLES = {"DMS Management", "System Manager"}
JOURNAL_PAYMENT_TYPES = ("Bank Entry", "Cash Entry")
DIRECTIONS = ("Receive", "Pay")


def _assert_can_read_payments():
	if not set(frappe.get_roles(frappe.session.user)) & PAYMENT_READ_ROLES:
		frappe.throw(_("Only Management can view payments."), frappe.PermissionError)


def _voucher_type_col(doctype: str, alias: str, fallback: str) -> str:
	"""Tally's own voucher type where the migration added it, else ERPNext's."""
	return f"{alias}.`tally_voucher_type`" if frappe.get_meta(doctype).has_field("tally_voucher_type") else fallback


def _union_sql() -> str:
	return f"""
		select 'Payment Entry' as source, pe.name, pe.posting_date, pe.payment_type as direction,
			pe.party_type, pe.party, coalesce(pe.party_name, pe.party) as party_name,
			pe.paid_amount as amount, pe.mode_of_payment as mode, pe.reference_no,
			{_voucher_type_col("Payment Entry", "pe", "''")} as voucher_type,
			pe.remarks
		from `tabPayment Entry` pe
		where pe.docstatus = 1 and pe.payment_type in ('Receive', 'Pay')
		union all
		select 'Journal Entry', je.name, je.posting_date,
			if(cash.net >= 0, 'Receive', 'Pay'),
			coalesce(other.party_type, ''), coalesce(other.party, oa.account_name, other.account),
			coalesce(other.party, oa.account_name, other.account),
			abs(cash.net), coalesce(ca.account_name, cash.account), je.cheque_no,
			{_voucher_type_col("Journal Entry", "je", "je.voucher_type")},
			je.user_remark
		from `tabJournal Entry` je
		join (
			select jea.parent, sum(jea.debit - jea.credit) as net, min(jea.account) as account
			from `tabJournal Entry Account` jea
			join `tabAccount` a on a.name = jea.account and a.account_type in ('Bank', 'Cash')
			group by jea.parent
		) cash on cash.parent = je.name
		left join (
			select jea.parent,
				max(jea.party_type) as party_type,
				max(nullif(jea.party, '')) as party,
				min(jea.account) as account
			from `tabJournal Entry Account` jea
			join `tabAccount` a on a.name = jea.account and ifnull(a.account_type, '') not in ('Bank', 'Cash')
			group by jea.parent
		) other on other.parent = je.name
		left join `tabAccount` ca on ca.name = cash.account
		left join `tabAccount` oa on oa.name = other.account
		where je.docstatus = 1 and je.voucher_type in {JOURNAL_PAYMENT_TYPES}
			and abs(cash.net) >= 0.005
	"""


@frappe.whitelist(methods=["GET"])
def list_payments(
	search: str | None = None,
	direction: str | None = None,
	party: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	limit: int = 20,
	offset: int = 0,
):
	_assert_can_read_payments()
	limit, offset = clamp(limit, offset)
	if direction and direction not in DIRECTIONS:
		frappe.throw(_("Invalid direction: {0}").format(direction), frappe.ValidationError)

	conditions = ["1 = 1"]
	params: dict = {}
	if direction:
		conditions.append("direction = %(direction)s")
		params["direction"] = direction
	if party:
		conditions.append("party = %(party)s")
		params["party"] = party
	if from_date:
		conditions.append("posting_date >= %(from_date)s")
		params["from_date"] = from_date
	if to_date:
		conditions.append("posting_date <= %(to_date)s")
		params["to_date"] = to_date
	if search:
		conditions.append(
			"(name like %(search)s or party like %(search)s or party_name like %(search)s"
			" or reference_no like %(search)s or remarks like %(search)s)"
		)
		params["search"] = f"%{search.strip()}%"
	where = " and ".join(conditions)
	base = f"select * from ({_union_sql()}) payments where {where}"

	total = frappe.db.sql(f"select count(*) from ({base}) counted", params)[0][0]
	sums = frappe.db.sql(
		f"select direction, sum(amount) from ({base}) summed group by direction", params
	)
	rows = frappe.db.sql(
		f"{base} order by posting_date desc, name desc limit %(limit)s offset %(offset)s",
		{**params, "limit": limit, "offset": offset},
		as_dict=True,
	)
	totals = {d: float(s or 0) for d, s in sums}
	return {
		"items": [
			{
				"id": r.name,
				"source": r.source,
				"date": r.posting_date,
				"direction": r.direction,
				"partyType": r.party_type or None,
				"party": r.party,
				"partyName": r.party_name,
				"amount": r.amount,
				"mode": r.mode,
				"referenceNo": r.reference_no,
				"voucherType": r.voucher_type or None,
			}
			for r in rows
		],
		"total": total,
		"totalReceived": totals.get("Receive", 0.0),
		"totalPaid": totals.get("Pay", 0.0),
		"limit": limit,
		"offset": offset,
	}


def _account_names(accounts: list[str]) -> dict[str, str]:
	"""Account id ("Cash - PI") -> its own account_name ("Cash"), the name as it was in Tally."""
	rows = frappe.get_all("Account", filters={"name": ["in", [a for a in accounts if a]]}, fields=["name", "account_name"])
	return {r.name: r.account_name or r.name for r in rows}


@frappe.whitelist(methods=["GET"])
def get_payment(source: str, payment: str):
	_assert_can_read_payments()
	if source == "Payment Entry":
		doc = frappe.get_doc("Payment Entry", payment)
		names = _account_names([doc.paid_from, doc.paid_to])
		return {
			"id": doc.name,
			"source": source,
			"date": doc.posting_date,
			"direction": doc.payment_type,
			"partyType": doc.party_type,
			"party": doc.party,
			"partyName": doc.party_name,
			"amount": doc.paid_amount,
			"mode": doc.mode_of_payment,
			"referenceNo": doc.reference_no,
			"referenceDate": doc.reference_date,
			"paidFrom": names.get(doc.paid_from, doc.paid_from),
			"paidTo": names.get(doc.paid_to, doc.paid_to),
			"remarks": doc.remarks,
			"allocations": [
				{"doctype": r.reference_doctype, "name": r.reference_name, "amount": r.allocated_amount}
				for r in doc.references
			],
			"lines": [],
		}
	if source == "Journal Entry":
		doc = frappe.get_doc("Journal Entry", payment)
		if doc.voucher_type not in JOURNAL_PAYMENT_TYPES:
			frappe.throw(_("{0} is not a bank or cash journal.").format(payment), frappe.ValidationError)
		cash_accounts = set(
			frappe.get_all(
				"Account",
				filters={"name": ["in", [r.account for r in doc.accounts]], "account_type": ["in", ["Bank", "Cash"]]},
				pluck="name",
			)
		)
		net = sum((r.debit or 0) - (r.credit or 0) for r in doc.accounts if r.account in cash_accounts)
		counter = next((r for r in doc.accounts if r.account not in cash_accounts and r.party), None) or next(
			(r for r in doc.accounts if r.account not in cash_accounts), None
		)
		names = _account_names([r.account for r in doc.accounts])
		counter_name = (counter.party or names.get(counter.account, counter.account)) if counter else None
		cash_account = next(iter(sorted(cash_accounts)), None)
		return {
			"id": doc.name,
			"source": source,
			"date": doc.posting_date,
			"direction": "Receive" if net >= 0 else "Pay",
			"partyType": (counter.party_type or None) if counter else None,
			"party": counter_name,
			"partyName": counter_name,
			"mode": names.get(cash_account, cash_account) if cash_account else None,
			"referenceNo": doc.cheque_no,
			"referenceDate": doc.cheque_date,
			"amount": abs(net),
			"remarks": doc.user_remark,
			"allocations": [],
			"lines": [
				{
					"account": names.get(row.account, row.account),
					"partyType": row.party_type or None,
					"party": row.party or None,
					"debit": row.debit_in_account_currency,
					"credit": row.credit_in_account_currency,
				}
				for row in doc.accounts
			],
		}
	frappe.throw(_("Invalid source: {0}").format(source), frappe.ValidationError)
