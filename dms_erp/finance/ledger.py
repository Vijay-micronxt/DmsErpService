"""Party balances read from the general ledger -- the one source that includes opening
balances, receipts and journals whether or not they were ever allocated against a
specific invoice. Sales Invoice.outstanding_amount is not that: migrated Tally receipts
were mostly posted as journals with no invoice allocation, so per-invoice outstanding
overstates what a dealer actually owes.
"""

import frappe


def party_balances(party_type: str, parties: list[str], company: str) -> dict[str, float]:
	"""Debit minus credit per party -- positive means the party owes the company."""
	if not parties:
		return {}
	rows = frappe.db.sql(
		"""
		select party, sum(debit) - sum(credit) as balance
		from `tabGL Entry`
		where party_type = %(party_type)s and party in %(parties)s and company = %(company)s and is_cancelled = 0
		group by party
		""",
		{"party_type": party_type, "parties": parties, "company": company},
		as_dict=True,
	)
	return {r.party: float(r.balance or 0) for r in rows}
