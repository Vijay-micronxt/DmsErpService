# Finance

Phase 6: Damage/insurance claims with accounting tie-in, unloading labour payment.
Implemented.

- **Insurance Claim** (`claims_api.py`, `doctype/insurance_claim`) — a genuine
  custom doctype (no ERPNext equivalent). One per Damage→Insurance Claim Stock
  Entry (Phase 3's `transfer_api.py`); filing a claim writes back to that
  transfer's `custom_claim_ref` so it becomes a real, queryable link instead of
  free text. Item/batch/qty are read from the linked Stock Entry, never
  duplicated — Stock Entries are immutable once submitted, so that's always safe.
- **Unloading Charge** (`unloading_api.py`, `doctype/unloading_charge`) — a
  genuine custom doctype, one per Inward Truck (Phase 3). `boxes` is read from
  the linked truck rather than re-entered; the charge amount (`boxes × rate`) is
  computed on read, never stored — same pattern as pricing's `landingCost`.

GL posting is optional and config-gated (Phase 14) rather than assuming a Chart
of Accounts, via a new **DMS Accounting Settings** Single doctype
(`doctype/pacific_accounting_settings`):

- `post_accounting_entries` (Check, default unchecked) — while off, settling a
  claim or marking an unloading charge paid is a pure status/amount update,
  exactly as it always was. No accounts need to be configured; the app stays
  fully usable/demoable before an accountant has picked a CoA.
- `default_company`, `default_bank_account`, `insurance_claim_receivable_account`,
  `insurance_settlement_variance_account` (optional — only required when a
  settlement's amount differs from the claimed amount), `unloading_expense_account`.

When the flag is on, `finance/accounting.py` verifies the accounts a given action
needs are configured — raising a clear `ValidationError` naming what's missing
rather than guessing or defaulting one — and only then posts:

- **Claim settlement** → a **Journal Entry**: debit the bank account for what was
  actually received, credit the claim-receivable account for the full claimed
  amount, and (only if the two differ) route the delta through the variance
  account as a loss or gain. `Insurance Claim.settlement_journal_entry` links back.
- **Unloading payment** → a **Payment Entry** (Internal Transfer — there's no real
  ERPNext Party for a labour contractor): debit the unloading expense account,
  credit the bank account. `Unloading Charge.payment_entry` links back.

Turning posting on later is a config change (check one box, fill in a few
Account links), not a redeploy — same reasoning as Quotation freight not being
wired into Sales Taxes and Charges in Phase 5.

Writes restricted to DMS Warehouse/Management (or System Manager) for
claims/unloading; DMS Accounting Settings itself is Management/System
Manager only. Everyone reads the operational records.
