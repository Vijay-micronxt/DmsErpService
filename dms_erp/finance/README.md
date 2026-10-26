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

- **Year-end claim reconciliation notice (BRD C.8.2, 26 Oct 2026)** — the
  31-Mar-removal/1-Apr-reinstatement *journal* cycle is still not automated
  (this app never posts to the GL at claim filing time, only at settlement, so
  there's no open ledger balance for a journal to reverse — that needs its own
  filing-time GL posting decision first, bigger than this module). What *is*
  now automated: `notify_year_end_claim_reconciliation` (scheduled daily, a
  no-op every day except 31 March/1 April) alerts Finance/Management with the
  count and total of everything `claims_pending_year_end_reconciliation`
  still shows open, so that report actually gets looked at on the two days it
  matters instead of sitting unopened.
