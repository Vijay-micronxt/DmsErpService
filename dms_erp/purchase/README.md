# Purchase

Phase 4: Purchase Orders, Requirements / reorder-suggestion engine. Implemented.

- **Purchase Orders** (`po_api.py`) — ERPNext's native `Purchase Order` /
  `Purchase Order Item`, submitted immediately on creation (no separate draft/
  approval step — "Raise PO" is one action, matching the frontend). Only
  supplier-confirmed readiness (`readyQty`, BRD §13.2) and free-text remarks
  are Custom Fields; `receivedQty` is ERPNext's own native `received_qty`,
  kept accurate because `warehouse/allocation_api.py` links the Purchase
  Receipts it posts back to the PO line they fulfill.
- **Reorder-suggestion engine** (`reorder_api.py`) — real today for the
  signals that exist: current stock (Phase 3's live Bin aggregate) and the
  safety-stock/non-reorderable logic. `missedDemandQty`, `pendingInquiryQty`
  and `recentRetailSalesQty` are honest zero placeholders until Phase 5
  (Inquiries/Orders) supplies real data — same "stub what we don't have yet"
  approach used for `Product.stockQty` before Phase 3 landed.

`Product.stockQty` (Phase 2) now reads real data from Phase 3's Bin aggregate
instead of a `0` stub, now that Warehouse exists.
