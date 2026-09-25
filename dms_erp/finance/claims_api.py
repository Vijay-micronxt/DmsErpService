"""Insurance Claims (BRD §15 accounting tie-in; BRD C.8 "Claim Voucher unification").

"A 'Damage -> Insurance Claim' bay transfer (warehouse/transfer_api.py) moves the
physical stock; this module is the companion financial record — the claim is filed
as a receivable the moment stock moves, then carried through to settlement or
rejection" (matching the frontend's own module docstring). No ERPNext doctype models
this, so Insurance Claim is a genuine custom doctype — one per Damage->Insurance
Claim Stock Entry (never duplicated), linking back so that transfer's own
`custom_claim_ref` becomes a real, queryable reference instead of free text.

`claim_type` (Insurance/Transit/Shortage) unifies what the BRD describes as three
separate concerns onto this one doctype rather than three parallel ones — Insurance/
Transit both originate from a Damage→Insurance Claim Stock Entry (stock_entry
required, supplier derived from it); a Shortage claim (identified at receipt, no
damage-bay transfer involved) has no stock_entry, so `supplier` must be passed
directly instead. `accumulated_claims_by_supplier` is the "small values accumulated
by supplier before a voucher is raised" read the BRD describes — filing/approval
still happens per-claim; this is a grouping view over what's pending, not a
different write path.

GL posting on settlement is optional and config-gated (Phase 14) rather than
guessing a Chart of Accounts: `update_claim_status` always updates the status/
settled-amount fields, exactly as before this existed. Only when `DMS
Accounting Settings.post_accounting_entries` is checked does it additionally post
a Journal Entry (see finance/accounting.py) — and if the required accounts aren't
configured, that raises a clear ValidationError rather than posting to a guessed
account. This keeps the app fully usable before an accountant has picked a CoA;
turning on GL posting later is a config change, not a redeploy.

BRD C.8.2's year-end (31 Mar removal / 1 Apr reinstatement) journal cycle is
deliberately NOT built here: this app has never posted anything to the GL at
*filing* time (only at settlement), so there is no open receivable balance in the
ledger for a year-end job to reverse yet — building one would post a journal
against a balance that doesn't actually exist in the books. Closing that gap needs
a filing-time GL posting decision first, which is bigger than "claim voucher
unification" and belongs in its own conversation with Pacific/the accountant.
`claims_pending_year_end_reconciliation` gives finance the honest, read-only
version instead: everything still Filed/Approved as of a given date, for a human
to reconcile until that decision is made.
"""

import frappe
from frappe import _
from frappe.utils import getdate, today

from dms_erp.finance import accounting
from dms_erp.pagination import clamp

CLAIM_WRITE_ROLES = {"DMS Warehouse", "DMS Management", "System Manager"}
DAMAGE_TO_CLAIM_TRANSFER_TYPE = "Damage→Insurance Claim"
CLAIM_TYPES = ["Insurance", "Transit", "Shortage"]
OPEN_CLAIM_STATUSES = ["Filed", "Approved"]


def _assert_can_manage_claims():
	if not set(frappe.get_roles(frappe.session.user)) & CLAIM_WRITE_ROLES:
		frappe.throw(_("Only Warehouse or Management can manage insurance claims."), frappe.PermissionError)


def _stock_entry_snapshot(stock_entry: str | None) -> dict:
	if not stock_entry:
		return {"itemCode": None, "batchNumber": None, "qty": None}
	row = frappe.get_doc("Stock Entry", stock_entry).items[0]
	return {"itemCode": row.item_code, "batchNumber": row.batch_no, "qty": row.qty}


def _supplier_from_stock_entry(stock_entry: str) -> str | None:
	"""Best-effort trace back to the originating Bay Allocation's supplier for the
	same item+batch — Stock Entry itself carries no supplier field. None (not a
	throw) when it can't be traced; accumulation just won't group that claim."""
	row = frappe.get_doc("Stock Entry", stock_entry).items[0]
	return frappe.db.get_value("Bay Allocation", {"item": row.item_code, "batch_no": row.batch_no}, "supplier")


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"claimRef": doc.name,
		"claimType": doc.claim_type,
		"supplier": doc.supplier,
		"stockEntry": doc.stock_entry,
		**_stock_entry_snapshot(doc.stock_entry),
		"insurer": doc.insurer,
		"claimAmount": doc.claim_amount,
		"approvedAmount": doc.approved_amount,
		"status": doc.status,
		"filedAt": doc.filed_at,
		"filedBy": doc.filed_by,
		"settlementMode": doc.settlement_mode,
		"settledAmount": doc.settled_amount,
		"settledAt": doc.settled_at,
		"settlementJournalEntry": doc.settlement_journal_entry,
		"netLoss": (doc.claim_amount or 0) - (doc.settled_amount or 0) if doc.status == "Settled" else None,
		"responsibility": doc.responsibility,
		"remarks": doc.remarks,
	}


def _claim_filters(status: str | None, claim_type: str | None = None) -> dict:
	filters = {}
	if status:
		filters["status"] = status
	if claim_type:
		filters["claim_type"] = claim_type
	return filters


def list_all_claims(status: str | None = None) -> list[dict]:
	"""Unpaginated — for internal callers (reports) that need the full result set,
	not a page of it. list_claims (the whitelisted endpoint) is the paginated one."""
	filters = _claim_filters(status)
	names = frappe.get_all("Insurance Claim", filters=filters, pluck="name", order_by="creation desc")
	return [_serialize(frappe.get_doc("Insurance Claim", name)) for name in names]


@frappe.whitelist(methods=["GET"])
def list_claims(status: str | None = None, claim_type: str | None = None, search: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = _claim_filters(status, claim_type)
	if search:
		filters["name"] = ["like", f"%{search}%"]
	total = frappe.db.count("Insurance Claim", filters=filters)
	names = frappe.get_all(
		"Insurance Claim", filters=filters, pluck="name", order_by="creation desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize(frappe.get_doc("Insurance Claim", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_claim(claim: str):
	return _serialize(frappe.get_doc("Insurance Claim", claim))


@frappe.whitelist(methods=["GET"])
def accumulated_claims_by_supplier(claim_type: str | None = None) -> list[dict]:
	"""BRD C.8: "small values are accumulated — always grouped by supplier/company —
	before a claim voucher is raised." A grouping view over still-open (Filed/
	Approved) claims; filing/approval itself is unchanged, per-claim."""
	rows = frappe.db.sql(
		"""
		select supplier, count(*) as claim_count, sum(claim_amount) as total_claimed
		from `tabInsurance Claim`
		where status in %(statuses)s {claim_type_clause}
		group by supplier
		order by total_claimed desc
		""".format(claim_type_clause="and claim_type = %(claim_type)s" if claim_type else ""),
		{"statuses": tuple(OPEN_CLAIM_STATUSES), "claim_type": claim_type},
		as_dict=True,
	)
	return [{"supplier": r.supplier, "claimCount": r.claim_count, "totalClaimed": r.total_claimed} for r in rows]


@frappe.whitelist(methods=["GET"])
def claims_pending_year_end_reconciliation(as_of=None) -> list[dict]:
	"""BRD C.8.2's 31-Mar removal/1-Apr reinstatement journal cycle isn't automated
	yet (see module docstring for why) — this is the honest, read-only version: every
	claim still Filed/Approved as of `as_of` (defaults to today), for a human to
	reconcile."""
	as_of = getdate(as_of) if as_of else getdate(today())
	names = frappe.get_all(
		"Insurance Claim",
		filters={"status": ["in", OPEN_CLAIM_STATUSES], "filed_at": ["<=", as_of]},
		pluck="name",
		order_by="filed_at asc",
	)
	return [_serialize(frappe.get_doc("Insurance Claim", name)) for name in names]


@frappe.whitelist(methods=["POST"])
def file_claim(
	claim_amount: float,
	claim_type: str = "Insurance",
	stock_entry: str | None = None,
	supplier: str | None = None,
	insurer: str | None = None,
	remarks: str | None = None,
):
	_assert_can_manage_claims()

	if claim_type not in CLAIM_TYPES:
		frappe.throw(_("Invalid claim_type: {0}").format(claim_type), frappe.ValidationError)

	if stock_entry:
		entry = frappe.get_doc("Stock Entry", stock_entry)
		if entry.custom_transfer_type != DAMAGE_TO_CLAIM_TRANSFER_TYPE:
			frappe.throw(_("Stock Entry {0} is not a Damage → Insurance Claim transfer.").format(stock_entry), frappe.ValidationError)
		if frappe.db.exists("Insurance Claim", {"stock_entry": stock_entry}):
			frappe.throw(_("A claim has already been filed for this transfer."), frappe.DuplicateEntryError)
		supplier = supplier or _supplier_from_stock_entry(stock_entry)
	elif not supplier:
		# No stock_entry to derive supplier from (typically a Shortage claim, BRD
		# C.8.3) -- accumulation (BRD C.8's whole point) needs it up front.
		frappe.throw(_("supplier is required when filing a claim with no stock_entry."), frappe.ValidationError)

	doc = frappe.get_doc(
		{
			"doctype": "Insurance Claim",
			"claim_type": claim_type,
			"supplier": supplier,
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

	if stock_entry:
		frappe.db.set_value("Stock Entry", stock_entry, "custom_claim_ref", doc.name)

	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_claim_status(
	claim: str,
	status: str,
	settled_amount: float | None = None,
	approved_amount: float | None = None,
	settlement_mode: str | None = None,
	responsibility: str | None = None,
):
	_assert_can_manage_claims()

	doc = frappe.get_doc("Insurance Claim", claim)
	doc.status = status
	if approved_amount is not None:
		doc.approved_amount = approved_amount
	if responsibility:
		doc.responsibility = responsibility
	if status == "Settled":
		doc.settlement_mode = settlement_mode or doc.settlement_mode
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
