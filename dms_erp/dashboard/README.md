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
all four), plus one role-agnostic entry point in front of them:

- `get_dashboard` — no role param, no `<method>` name for the frontend to hardcode:
  resolves *every* DMS role the caller holds via `auth.utils.resolve_app_roles` (the
  exact same list, highest-privilege first, `auth.api.login`'s `user.app_roles` is
  computed from -- a user holding both DMS Management and DMS Sales gets a dashboard
  for each, not just the higher-priority one), then for each role reads that role's
  ERPNext-**native** `Dashboard` doc (`DMS Sales`/`DMS Purchase`/`DMS Warehouse`/
  `DMS Management` — same name as the DMS Role doc itself) and returns
  `{"dashboards": [{"role", "widgets"}, ...]}` — one entry per role (a single-role
  user still gets a one-entry array, so there's one response shape for everyone) —
  the Number Card/Dashboard Chart widgets a System Manager configured on it via the
  desk UI, permission-filtered per widget by Frappe's own
  `Dashboard.get_permitted_cards`/`get_permitted_charts`. Unlike the four functions
  below, this endpoint's content lives entirely in ERPNext config, not code: adding,
  removing, or reordering a KPI needs no deploy, only editing that Dashboard doc.
  A missing/unconfigured Dashboard doc returns an empty `widgets` list for that
  entry, not an error — this endpoint ships ahead of that setup. It no longer calls
  the four functions below at all; they remain independently callable but exist now
  mainly as reference implementations of the same aggregations, in case a native Number
  Card/Chart can't express one (e.g. `missedDemandValue`'s per-row price lookup).
  A System Manager-only account (the admin escape-hatch `auth/api.py` documents —
  no `DMS *` role at all, so `resolve_primary_role` alone would find nothing) is
  routed to `"management"` instead of rejected.
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
