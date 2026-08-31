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

- **`sales_reports.py`** — Dealer Inquiry Report, Missed Demand Report, and
  (Phase 17) Retail vs Bulk Report — order count/value grouped by `Sales Order.
  custom_order_channel` (Phase 15). Entirely unblocked by that field; before it
  existed there was nothing to group by.
- **`warehouse_reports.py`** — Bay Occupancy Report, Visual Stock Balance.
- **`purchase_reports.py`** — Purchase Reorder Planning Report, Purchase Pickup
  Plan, and (Phase 17) Inquiry-to-PO Mapping Report (joins `Purchase Order.
  custom_source_inquiry` back to its Inquiry — only POs raised via `sales.
  inquiry_api.convert_to_purchase_requirement` appear; a directly-raised PO has
  nothing to map) and PO Pending Report (wraps `po_api.list_pending_po_lines`).
- **`finance_reports.py`** — Damage & Insurance Report, Claimable Value Report,
  Unloading Payment Report.
- **`catalog_reports.py`** (Phase 17) — Pricing & CSP Report (CSP = Customer
  Suggested Price, read as the approved proposal's own `suggestedPrice` field —
  landing cost × (1 + margin %) — not a second lookup against the live dealer
  price) and Fast/Slow-Moving Product Report (ranks the whole catalog by the same
  trailing-window Retail sales velocity the reorder engine computes per-item, via
  a new public `reorder_api.sales_velocity_by_item()` wrapper so both read the
  same signal instead of two copies of the query).

BRD reports still to land: Dealer Activity, Product Activity (Phase 18);
Duplicate Inquiry, Stock Clearance Suggestion, Display Replacement Suggestion
(Phase 19); Forecasting Dashboard (Phase 20).
