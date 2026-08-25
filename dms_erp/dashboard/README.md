# Dashboard

Phase 8: role-based operating dashboards. Implemented.

Not part of Phase 0's original module list — like `sales` in Phase 5, this phase's
scope didn't map onto any of the seven modules bootstrapped at the start, so it gets
its own. Unlike every other module, there is no doctype here at all: `api.py` is
pure read/aggregation over everything Phases 0–7 already built. Nothing is stored.

Most of `pacific-tileflow`'s `dashboard.tsx` is, honestly, hardcoded demo numbers —
a literal `value="7"` for "Today's Inquiries", a literal `inr(4820000)` for "Total
Sales" — not real computed KPIs, unlike `warehouse-dashboard.ts`'s `warehouseKpis`/
`warehouseAlerts`, which were real from the start (and are ported here as-is, now
reading live Warehouse/Bin/Inward Truck/Pick Task data instead of a mock store).
Everywhere else the frontend hardcoded a number, this module computes the real
thing instead, now that every other phase's data actually exists to compute it
from — and stubs, clearly, only what would need data this app was never scoped to
produce:

- **`outstandingReceivables` is a hard `0`** — real accounts-receivable needs a
  Sales Invoice + Payment Entry ledger, and no phase in this 8-phase plan ever
  posts one (the BRD flow this app implements stops at "Dispatched").
- **The management dashboard's credit-limit alert uses order value, not true
  receivables** — `Customer.credit_limit` (native) against the sum of a dealer's
  submitted Sales Order value, which is a real, honest signal but not the same
  thing as actual outstanding AR net of delivery/payment.
- **"Damage awaiting claim"** is computed properly rather than stubbed: for each
  lot sitting in a damage-type bay, it traces back through Stock Ledger Entry to
  the Stock Entry that moved it there and checks whether that transfer's
  `custom_claim_ref` (Phase 6) is set.

Four endpoints, one per role, each gated to that role (or Management, who sees
all four):

- `sales_dashboard` — today's inquiries, pending quotations, orders this month,
  missed-demand value, a real 30-day inquiry trend, actionable inquiries.
- `warehouse_dashboard` — the ported `warehouseKpis`/`warehouseAlerts`, plus
  today's incoming trucks.
- `purchase_dashboard` — pending/delayed POs (computed from `received_qty` vs
  ordered qty), this week's pickup plans, a real reorder-suggestion count
  (Phase 4), a real monthly purchase-value trend, materials ready for pickup.
- `management_dashboard` — sales MTD, claimable insurance value (Phase 6), top
  moving item (real sales-velocity ranking, not the frontend's hardcoded
  `"PVT-6060"`), sales by dealer, and the credit-exposure alert above.
