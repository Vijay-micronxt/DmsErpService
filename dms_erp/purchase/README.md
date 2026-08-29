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
- **Reorder-suggestion engine** (`reorder_api.py`) — fully real as of Phase 10.
  `currentStock` and the safety-stock/non-reorderable logic were real from Phase 4
  (Phase 3's live Bin aggregate); `missedDemandQty`, `pendingInquiryQty` and
  `recentRetailSalesQty` were honest zero placeholders until Phase 5 (Inquiries/
  Orders) existed to supply them, and Phase 10 wires them up: the first two are
  grouped `Inquiry.qty` sums over the appropriate slices of Inquiry's 10-state
  lifecycle, and `recentRetailSalesQty` is a trailing-180-day submitted Sales
  Order Item sum, converted to a daily rate and projected across the item's own
  `lead_time_days` to add a real "expected demand during lead time" term on top
  of the flat safety-stock floor.

`Product.stockQty` (Phase 2) now reads real data from Phase 3's Bin aggregate
instead of a `0` stub, now that Warehouse exists.
