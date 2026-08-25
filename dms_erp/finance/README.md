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

Neither doctype posts to the General Ledger (no Journal Entry, no Payment Entry)
— that would require assuming specific Chart of Accounts accounts exist on the
target site, which this app can't know. Both are tracked as real financial
records (receivable/settled amounts, payment status) even so; real GL posting is
a natural future refinement once the site's accounts are known.

Writes restricted to Pacific Warehouse/Management (or System Manager); everyone
reads.
