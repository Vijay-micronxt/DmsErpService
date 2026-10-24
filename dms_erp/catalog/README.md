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
- **Whole-catalog load** (`list_all_products_bulk` / `_serialize_many`) — the staff
  app keeps every product in one shared in-memory store (29 screens read it, and its
  pickers/search are client-side), but it was loading that store from
  `list_products`' first page, so every screen saw 20 of ~4,500 migrated items.
  `_serialize_many` builds the exact `_serialize` dict for any number of items in a
  fixed handful of queries (Item, Bin, Item Price, Product Series, Item Alternative
  and the two child tables) — about 1–2 s for all 4,340 migrated items, versus ~95 s
  one-by-one. It must stay key-for-key identical to `_serialize` (one `Product` type
  on the frontend); `test_products.test_bulk_serializer_matches_one_by_one` pins
  that, and it was also checked item-by-item against all 4,340 migrated items. Note
  `get_all` skips `Document`'s numeric casting, so numeric columns are cast the same
  way a loaded doc would be.
- **All price lists for an item** (`get_item_prices`) — every *selling* Price List
  rate the item has, view-only, for the item page and pricing screen. The three tier
  rows (`pricing.api.get_dealer_tier_prices`) hid the Tally-migrated lists, including
  the one a dealer is tagged to via `Customer.default_price_list` — so the rate a
  tagged dealer is actually billed was visible nowhere. Filters on the Price List's
  own `selling` flag rather than the copy on each Item Price row, since bulk-imported
  rows aren't guaranteed to carry it.
- **Dealer Catalog** (`dealer_catalog_api.py` + `doctype/dealer_catalog`) — a custom
  doctype (no ERPNext equivalent) mapping a dealer (`Customer`) to the set of items
  they're allowed to order. No assignment yet = full catalog visible/orderable,
  matching the frontend's fallback behavior. `is_visible` is a pure assignment check;
  `catalog_for` — "what a dealer can actually order/quote for" — additionally filters
  to currently-sellable items (Phase 11), since a Pulled Back item can't be quoted for
  any dealer regardless of assignment. `sales/quotation_api.create_quotation` also
  enforces sellability directly at the line level, because assignment isn't
  retroactively cleaned up when an item's lifecycle status changes later.
  `browsable_catalog_for` (ISS-2026-00644) is the wider, assignment-blind sibling —
  every currently-sellable item, period — that `sales/inquiry_api._create_inquiry`,
  the dealer portal's `get_catalog`/`get_item`/`resolve_code`, and WhatsApp's
  `resolve_item_by_name` (catalog/api.py) now use so a dealer can see and inquire on
  an unassigned item; `catalog_for`'s narrower assigned set is still what gates
  actually *ordering* it (`dealer_portal_api.convert_to_order`, comms.flow_api's
  WhatsApp order-placement path, and quotation_api unchanged).
- **First *hide* on an unassigned dealer starts from everything**
  (`_materialize_full_catalog`) — a dealer with no Dealer Catalog sees everything, but
  hiding one item used to create an *empty* catalog and remove the item from it,
  leaving the dealer able to order nothing (a real report: 4,338 orderable → 0). The
  first hide (single item or a category's "Hide all") now seeds the new catalog with
  every Item and removes from that. A first *show* still creates a one-item catalog,
  as before — that's `sample_api.issue_sample`'s path, where BRD C.1.5 has a dealer's
  catalog start from their first issued sample. Consequence worth knowing: once a
  dealer has a catalog, items created later are not added to it automatically.
  `list_catalog_assignments` returns every existing catalog in one call (a dealer
  missing from it has none = sees everything); the staff app uses it instead of
  calling `catalog_for` once per dealer.

`stockQty`, `bay` and `lastSoldDays` are stubbed in API responses — they come from
Phase 3 (Warehouse/Stock) and Phase 5 (Sales) respectively.

- **Categories** — `list_item_group_names` returns every leaf category unpaginated (the staff app had been
  using `list_item_groups`' first page of 20 as "all categories", so Dealer Catalogs missed every item outside
  them on a migrated site with ~1,100); `create_item_group` adds one from the product form's picker.
