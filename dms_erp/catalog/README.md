# Catalog

Phase 2: Product / Item Master (5-state discontinuation lifecycle) and dealer-specific
catalog visibility. Implemented.

- **Product / Item Master** (`api.py`) — backed by ERPNext's native `Item` doctype
  plus Custom Fields for the attributes ERPNext has no equivalent for (size, finish,
  color, series, swatch color, pieces/sqft/weight per box, discontinuation status).
  `category` is the native `Item Group`; `leadTimeDays` is Item's own
  `lead_time_days`; `altItemId` is the native `Item Alternative` doctype — none of
  those needed a custom field.
- **Dealer Catalog** (`dealer_catalog_api.py` + `doctype/dealer_catalog`) — a custom
  doctype (no ERPNext equivalent) mapping a dealer (`Customer`) to the set of items
  they're allowed to see. No assignment yet = full catalog visible, matching the
  frontend's fallback behavior.

`stockQty`, `bay` and `lastSoldDays` are stubbed in API responses — they come from
Phase 3 (Warehouse/Stock) and Phase 5 (Sales) respectively.
