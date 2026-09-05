# Catalog

Phase 2: Product / Item Master (5-state discontinuation lifecycle) and dealer-specific
catalog visibility. Implemented.

- **Product / Item Master** (`api.py`) — backed by ERPNext's native `Item` doctype
  plus Custom Fields for the attributes ERPNext has no equivalent for (size, finish,
  color, series, swatch color, pieces/sqft/weight per box, discontinuation status).
  `category` is the native `Item Group`; `leadTimeDays` is Item's own
  `lead_time_days`; `altItemId` is the native `Item Alternative` doctype — none of
  those needed a custom field. `list_item_groups` is the read endpoint for
  `category`'s options — the native `Item Group` tree, filtered to `is_group: 0`
  (the leaf categories `catalog/setup.py` seeds; the root "All Item Groups" is
  excluded), for populating the category field on product forms. No new doctype
  or field, and no separate detail endpoint — a category has nothing beyond its
  name and parent worth a second call for. `list_products` and `list_item_groups`
  both take `search`/`limit`/`offset` and return `{"items", "total", "limit",
  "offset"}` rather than a bare array (`dms_erp.pagination.clamp` caps `limit` at
  100) — `reports/catalog_reports.py` needs the whole result set, not a page of
  it, so it calls the unpaginated `list_all_products` instead of the whitelisted
  endpoint. `list_products` also takes `category`/`status` (plain `Item` columns)
  and `supplier` — the one filter that isn't a column on `Item` at all, since
  supplier is only ever recorded on `Item Price Proposal` (autonamed `field:item`,
  so its `name` already *is* the item code — no join needed, just a second
  `get_all` intersected with any `dealer` restriction already in play).
- **Dealer Catalog** (`dealer_catalog_api.py` + `doctype/dealer_catalog`) — a custom
  doctype (no ERPNext equivalent) mapping a dealer (`Customer`) to the set of items
  they're allowed to see. No assignment yet = full catalog visible, matching the
  frontend's fallback behavior. `is_visible` is a pure assignment check; `catalog_for`
  — "what a dealer can actually inquire/quote for" — additionally filters to
  currently-sellable items (Phase 11), since a Pulled Back item can't be quoted for
  any dealer regardless of assignment. `sales/quotation_api.create_quotation` also
  enforces sellability directly at the line level, because assignment isn't
  retroactively cleaned up when an item's lifecycle status changes later.

`stockQty`, `bay` and `lastSoldDays` are stubbed in API responses — they come from
Phase 3 (Warehouse/Stock) and Phase 5 (Sales) respectively.
