# Reports

Phase 16+: BRD "Reports and Dashboards" — the *reports* half. No doctype lives
here, same as Dashboard; there is nothing to store, only to compute.

BRD groups "Reports and Dashboards" as one section, but they're a different shape
of the same idea. Phase 8's `dashboard` module is a fixed KPI snapshot per role's
home screen — four endpoints, no filters. This module is filterable, tabular
listings meant for a report screen: date ranges, dealer/item/status filters,
summary rollups alongside the rows. Kept as its own module rather than folded
into `dashboard/api.py` so that file doesn't grow to twenty-plus functions.

No new role gates on the reads — matching how every other list/get endpoint in
this app works (only writes are role-gated), unlike Dashboard's deliberate
per-role gating (a dashboard is "your homepage view"; a report is "whatever a
report screen asks for, filtered").

Every report here reads through the source module's own list/get functions —
`inquiry_api.list_inquiries`, `claims_api.list_claims`, `bay_api.list_bays`,
`reorder_api.reorder_suggestions`, etc. — rather than re-querying the underlying
doctypes directly. The report layer only adds date-range filtering, grouping, and
summary totals; data ownership stays with the domain module.

Two shared helpers were extracted from `dashboard/api.py::purchase_dashboard`
into `purchase/po_api.py` (`list_pending_po_lines`, `list_materials_ready_for_
pickup`) so the dashboard tile and the Phase 16 reports below compute from the
same source instead of two copies of the same SQL:

- **`sales_reports.py`** — Dealer Inquiry Report, Missed Demand Report; (Phase 17)
  Retail vs Bulk Report — order count/value grouped by `Sales Order.
  custom_order_channel` (Phase 15), entirely unblocked by that field; (Phase 18)
  Dealer Activity Report — a per-dealer rollup across Inquiry/Quotation/Sales
  Order/WhatsApp Message, the first report here no single existing list/get
  function could answer on its own; (Phase 19) Duplicate Inquiry Report — no
  BRD-specified duplicate rule existed, so this is a proposed one (two or more
  still-open inquiries, same dealer and item, logged within `window_days`,
  default 7, of each other), easy to retune via the `window_days` param.
- **`warehouse_reports.py`** — Bay Occupancy Report, Visual Stock Balance;
  (Phase 19) Stock Clearance Suggestion and Display Replacement Suggestion —
  neither had a BRD-specified formula either, so both are proposed heuristics
  built from small, named constants at the top of the file
  (`CLEARANCE_STOCK_MULTIPLE`, `CLEARANCE_MIN_AGE_DAYS`) rather than magic
  numbers buried in the query, specifically so the actual thresholds can be
  corrected without touching the logic around them. Clearance: current stock
  well above the safety-stock floor, barely moving, oldest batch aged past the
  threshold. Display replacement: an item on display that's not selling or is
  past Active in the discontinuation lifecycle, paired with the fastest-moving
  currently-sellable item in the same category not already displayed.
- **`purchase_reports.py`** — Purchase Reorder Planning Report, Purchase Pickup
  Plan; (Phase 17) Inquiry-to-PO Mapping Report (joins `Purchase Order.
  custom_source_inquiry` back to its Inquiry — only POs raised via `sales.
  inquiry_api.convert_to_purchase_requirement` appear; a directly-raised PO has
  nothing to map) and PO Pending Report (wraps `po_api.list_pending_po_lines`).
- **`finance_reports.py`** — Damage & Insurance Report, Claimable Value Report,
  Unloading Payment Report.
- **`catalog_reports.py`** — (Phase 17) Pricing & CSP Report (CSP = Customer
  Suggested Price, read as the approved proposal's own `suggestedPrice` field —
  landing cost × (1 + margin %) — not a second lookup against the live dealer
  price) and Fast/Slow-Moving Product Report (ranks the whole catalog by the same
  trailing-window Retail sales velocity the reorder engine computes per-item, via
  a public `reorder_api.sales_velocity_by_item()` wrapper so both read the same
  signal instead of two copies of the query); (Phase 18) Product Activity
  Report — the same cross-module-rollup shape as Dealer Activity, but per item
  (Inquiry count, Sales Order qty, Stock Entry transfer count, price-approval
  history length).

- **`forecasting.py`** (Phase 20) — Forecasting Dashboard. Same "BRD names the
  report, not the formula" situation as three of Phase 19's reports, taken to
  its logical simplest: a trailing 12-week average of Retail sales velocity,
  projected flat across the requested horizon. Deliberately does not model
  seasonality or trend — every row is flagged `"low"` confidence for exactly
  that reason, so a caller can't mistake this for more than it is. Swap the
  method in this one file once a real methodology is confirmed; nothing else in
  `reports` depends on how it's computed.

All 20 BRD reports now have an endpoint.
