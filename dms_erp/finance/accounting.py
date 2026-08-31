"""Shared accounting-settings gate for optional GL posting (claim settlement,
unloading payment). Everything here is conditional on `DMS Accounting
Settings.post_accounting_entries` — unchecked by default, so the app stays fully
usable/demoable before an accountant has picked a Chart of Accounts. Turning
posting on later is a config change (check a box, fill in a few Account links),
not a redeploy.

Both claims and unloading post through ERPNext's own Journal Entry / Payment
Entry doctypes — no bespoke ledger, same reasoning as every stock-effecting
action elsewhere in this app posting through native doctypes.
"""

import frappe
from frappe import _
from frappe.utils import today


def get_settings() -> "frappe.model.document.Document":
	return frappe.get_cached_doc("DMS Accounting Settings")


def _require(settings, *fieldnames: str):
	missing = [f for f in fieldnames if not settings.get(f)]
	if missing:
		frappe.throw(
			_("DMS Accounting Settings is missing: {0}. Configure these (or uncheck Post Accounting Entries) first.").format(
				", ".join(missing)
			),
			frappe.ValidationError,
		)


def post_claim_settlement(claim_amount: float, settled_amount: float, claim_ref: str) -> str | None:
	"""Journal Entry for a settled Insurance Claim: debit the bank account for what
	was actually received, credit the claim-receivable account for the full
	claimed amount. A delta between the two goes to the variance account —
	required only when there actually is a delta, since a clean settlement
	(settled == claimed) never needs one."""
	settings = get_settings()
	if not settings.post_accounting_entries:
		return None

	_require(settings, "default_company", "default_bank_account", "insurance_claim_receivable_account")

	delta = round(float(claim_amount) - float(settled_amount), 2)
	if delta and not settings.insurance_settlement_variance_account:
		frappe.throw(
			_(
				"Settled amount ({0}) differs from claimed amount ({1}) for {2}, but no Insurance Settlement "
				"Variance Account is configured in DMS Accounting Settings."
			).format(settled_amount, claim_amount, claim_ref),
			frappe.ValidationError,
		)

	accounts = [
		{"account": settings.default_bank_account, "debit_in_account_currency": settled_amount, "credit_in_account_currency": 0},
		{"account": settings.insurance_claim_receivable_account, "debit_in_account_currency": 0, "credit_in_account_currency": claim_amount},
	]
	if delta > 0:
		# Settled for less than claimed — the shortfall is a loss.
		accounts.append({"account": settings.insurance_settlement_variance_account, "debit_in_account_currency": delta, "credit_in_account_currency": 0})
	elif delta < 0:
		# Settled for more than claimed — the excess is a gain.
		accounts.append({"account": settings.insurance_settlement_variance_account, "debit_in_account_currency": 0, "credit_in_account_currency": -delta})

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": settings.default_company,
			"posting_date": today(),
			"user_remark": f"Insurance claim settlement — {claim_ref}",
			"accounts": accounts,
		}
	)
	je.insert(ignore_permissions=True)
	je.submit()
	return je.name


def post_unloading_payment(amount: float, charge_ref: str) -> str | None:
	"""Payment Entry (Internal Transfer — there's no real ERPNext Party for a
	labour contractor here) for a paid Unloading Charge: debit the unloading
	expense account, credit the bank account."""
	settings = get_settings()
	if not settings.post_accounting_entries:
		return None

	_require(settings, "default_company", "default_bank_account", "unloading_expense_account")

	pe = frappe.get_doc(
		{
			"doctype": "Payment Entry",
			"payment_type": "Internal Transfer",
			"company": settings.default_company,
			"posting_date": today(),
			"paid_from": settings.default_bank_account,
			"paid_to": settings.unloading_expense_account,
			"paid_amount": amount,
			"received_amount": amount,
			"reference_no": charge_ref,
			"reference_date": today(),
			"remarks": f"Unloading charge paid — {charge_ref}",
		}
	)
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe.name
