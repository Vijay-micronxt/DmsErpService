# Sales

Phase 5: Inquiries, Quotations, Orders, Picking. Implemented.

A new module — not part of the module list Phase 0 bootstrapped, since the original
plan didn't allocate one for this phase. Added here, matching the frontend's own
domain boundary (Inquiries/Quotations/Orders/Picking are one connected flow).

- **Inquiry** (`inquiry_api.py`, `doctype/inquiry`) — a genuine custom doctype; no
  ERPNext equivalent for "logged demand, not yet a sales document" with Pacific's
  exact 10-state status lifecycle. Unlike most other frontend `api/*.ts` modules,
  `pacific-tileflow`'s inquiries are still static read-only seed data with no
  create/update wired up client-side — this module builds the real, mutable backend
  the BRD describes; the frontend needs its own follow-up work to call it.
- **Quotation** (`quotation_api.py`) — ERPNext's native `Quotation` doctype,
  submitted immediately on creation (no draft step, same pattern as Phase 4's
  Purchase Order). Custom fields only for the retail markup % and freight (BRD
  §7.6) — freight stays a plain field rather than wired into Sales Taxes and
  Charges, since that needs a GL account this app can't assume exists on every
  site. Every line's rate is computed server-side from the approved dealer price
  (never client-supplied), and every item must be in the dealer's assigned catalog
  — both enforced as real gates, not just report-time checks.
- **Order** (`order_api.py`) — ERPNext's native `Sales Order`. Pacific's warehouse-
  fulfillment stages (Confirmed → Picking → Ready to Dispatch → Dispatched →
  Delivered/Cancelled) are a distinct operational flow layered on top via a custom
  field + a structured stage-history child table — advancing them does not itself
  create a Delivery Note or move stock; that's a natural future refinement. An
  order sourced directly from an Inquiry (no markup) and one converted from a
  Quotation (via ERPNext's own native mapper) are the only two creation paths,
  matching the frontend's `sourceType: "Inquiry" | "Quotation"`.
- **Pick Task** (`picking_api.py`, `doctype/pick_task`) — a genuine custom doctype.
  ERPNext's native Pick List exists but is a coarser, all-or-nothing document per
  pick run; Pacific wants one row per order line with a suggested bay, a partially-
  allocatable qty, a named picker and a simple Pending/Allocated/Picked status,
  which Pick List doesn't model at that granularity. Auto-allocation reads live
  stock straight from `warehouse/utils.py` — nothing here owns its own view of
  what's on hand. Entering the Picking stage on an order auto-creates its tasks.

Sales/Management manage Inquiries/Quotations/Orders; Warehouse/Management manage
Pick Tasks; everyone reads.
