# Purchase

Phase 4: Purchase Orders, Requirements / reorder-suggestion engine. Implemented.

- **Purchase Orders** (`po_api.py`) — ERPNext's native `Purchase Order` /
  `Purchase Order Item`, submitted immediately on creation (no separate draft/
  approval step — "Raise PO" is one action, matching the frontend). Only
  supplier-confirmed readiness (`readyQty`, BRD §13.2), free-text remarks, and
  (Phase 12) a `custom_source_inquiry` link are Custom Fields; `receivedQty` is
  ERPNext's own native `received_qty`, kept accurate because
  `warehouse/allocation_api.py` links the Purchase Receipts it posts back to the
  PO line they fulfill. `custom_source_inquiry` is set when a PO is raised via
  `sales.inquiry_api.convert_to_purchase_requirement` rather than directly.
  `list_pending_po_lines`/`list_materials_ready_for_pickup` (Phase 16) are public,
  line-level read helpers factored out of `dashboard.purchase_dashboard`'s inline
  SQL — shared by that dashboard tile and the `reports` module's Purchase Pickup
  Plan/PO Pending reports instead of existing as duplicated queries.
- **Reorder-suggestion engine** (`reorder_api.py`) — fully real as of Phase 10.
  `currentStock` and the safety-stock/non-reorderable logic were real from Phase 4
  (Phase 3's live Bin aggregate); `missedDemandQty`, `pendingInquiryQty` and
  `recentRetailSalesQty` were honest zero placeholders until Phase 5 (Inquiries/
  Orders) existed to supply them, and Phase 10 wires them up: the first two are
  grouped `Inquiry.qty` sums over the appropriate slices of Inquiry's 10-state
  lifecycle, and `recentRetailSalesQty` is a trailing-180-day submitted Sales
  Order Item sum, converted to a daily rate and projected across the item's own
  `lead_time_days` to add a real "expected demand during lead time" term on top
  of the flat safety-stock floor. Phase 13 adds `openPurchaseOrderQty`/
  `openPurchaseOrders` — a suggestion has no id of its own to tag a PO against
  (it's a live per-item computation, not a stored row), so "already ordered"
  means "sum of open PO lines for this item, still pending receipt", netted
  straight out of `raw_need` so `suggestedQty` drops once a PO already covers it.
  Phase 15: `recentRetailSalesQty` now actually filters to `Sales Order.
  custom_order_channel = 'Retail'` — this module's own docstring always claimed
  "retail channel only, bulk/project excluded per BRD §12.1", but until Phase 15
  added the channel field to Quotation/Sales Order, nothing existed to exclude.
  Phase 17 adds `sales_velocity_by_item()`, a public wrapper over the same
  grouped query, for the `reports` module's Fast/Slow-Moving Product Report.

`Product.stockQty` (Phase 2) now reads real data from Phase 3's Bin aggregate
instead of a `0` stub, now that Warehouse exists.
