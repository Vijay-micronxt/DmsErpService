"""Insurance Claims (BRD §15 accounting tie-in).

"A 'Damage -> Insurance Claim' bay transfer (warehouse/transfer_api.py) moves the
physical stock; this module is the companion financial record — the claim is filed
as a receivable the moment stock moves, then carried through to settlement or
rejection" (matching the frontend's own module docstring). No ERPNext doctype models
this, so Insurance Claim is a genuine custom doctype — one per Damage->Insurance
Claim Stock Entry (never duplicated), linking back so that transfer's own
`custom_claim_ref` becomes a real, queryable reference instead of free text.

GL posting on settlement is optional and config-gated (Phase 14) rather than
guessing a Chart of Accounts: `update_claim_status` always updates the status/
settled-amount fields, exactly as before this existed. Only when `Pacific
Accounting Settings.post_accounting_entries` is checked does it additionally post
a Journal Entry (see finance/accounting.py) — and if the required accounts aren't
configured, that raises a clear ValidationError rather than posting to a guessed
account. This keeps the app fully usable before an accountant has picked a CoA;
turning on GL posting later is a config change, not a redeploy.
"""

import frappe
from frappe import _
from frappe.utils import today

from dms_erp.finance import accounting

CLAIM_WRITE_ROLES = {"Pacific Warehouse", "Pacific Management", "System Manager"}
DAMAGE_TO_CLAIM_TRANSFER_TYPE = "Damage→Insurance Claim"


def _assert_can_manage_claims():
	if not set(frappe.get_roles(frappe.session.user)) & CLAIM_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage insurance claims."), frappe.PermissionError)


def _stock_entry_snapshot(stock_entry: str) -> dict:
	row = frappe.get_doc("Stock Entry", stock_entry).items[0]
	return {"itemCode": row.item_code, "batchNumber": row.batch_no, "qty": row.qty}


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"claimRef": doc.name,
		"stockEntry": doc.stock_entry,
		**_stock_entry_snapshot(doc.stock_entry),
		"insurer": doc.insurer,
		"claimAmount": doc.claim_amount,
		"status": doc.status,
		"filedAt": doc.filed_at,
		"filedBy": doc.filed_by,
		"settledAmount": doc.settled_amount,
		"settledAt": doc.settled_at,
		"settlementJournalEntry": doc.settlement_journal_entry,
		"remarks": doc.remarks,
	}


@frappe.whitelist(methods=["GET"])
def list_claims(status: str | None = None):
	filters = {"status": status} if status else {}
	names = frappe.get_all("Insurance Claim", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Insurance Claim", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def get_claim(claim: str):
	return _serialize(frappe.get_doc("Insurance Claim", claim))


@frappe.whitelist(methods=["POST"])
def file_claim(stock_entry: str, insurer: str, claim_amount: float, remarks: str | None = None):
	_assert_can_manage_claims()

	entry = frappe.get_doc("Stock Entry", stock_entry)
	if entry.custom_transfer_type != DAMAGE_TO_CLAIM_TRANSFER_TYPE:
		frappe.throw(_("Stock Entry {0} is not a Damage → Insurance Claim transfer.").format(stock_entry), frappe.ValidationError)
	if frappe.db.exists("Insurance Claim", {"stock_entry": stock_entry}):
		frappe.throw(_("A claim has already been filed for this transfer."), frappe.DuplicateEntryError)

	doc = frappe.get_doc(
		{
			"doctype": "Insurance Claim",
			"stock_entry": stock_entry,
			"insurer": insurer,
			"claim_amount": claim_amount,
			"status": "Filed",
			"filed_at": today(),
			"filed_by": frappe.session.user,
			"remarks": remarks,
		}
	)
	doc.insert(ignore_permissions=True)

	frappe.db.set_value("Stock Entry", stock_entry, "custom_claim_ref", doc.name)

	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_claim_status(claim: str, status: str, settled_amount: float | None = None):
	_assert_can_manage_claims()

	doc = frappe.get_doc("Insurance Claim", claim)
	doc.status = status
	if status == "Settled":
		doc.settled_amount = settled_amount if settled_amount is not None else doc.claim_amount
		doc.settled_at = today()
		doc.settlement_journal_entry = accounting.post_claim_settlement(doc.claim_amount, doc.settled_amount, doc.name)
	doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["GET"])
def claim_summary():
	claims = frappe.get_all("Insurance Claim", fields=["status", "claim_amount", "settled_amount"])
	receivable = sum(c.claim_amount for c in claims if c.status in ("Filed", "Approved"))
	settled = sum((c.settled_amount or 0) for c in claims if c.status == "Settled")
	rejected = sum(1 for c in claims if c.status == "Rejected")
	return {"receivable": receivable, "settled": settled, "rejected": rejected}
