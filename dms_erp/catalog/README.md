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
- **White-labeled discontinuation status labels** (`DMS Catalog Settings` /
  `DMS Catalog Status Label`, `catalog.utils.status_label`) — BRD C.10.5 wants 4
  client-specific display labels (Pacific's own wording: "PDC"/"Against Order")
  over the real, unchanged 5-state internal lifecycle above. "PDC" literally
  stands for "Pacific-discontinued" — this app is white-labeled across clients
  (`CLAUDE.md`), so that wording is config, not code. Every status's *internal*
  value (what `is_reorderable`/`is_sellable`/`withdrawal_api`/filters all match
  on) is untouched; only the *displayed* label is configurable, via a per-status
  row on the new `DMS Catalog Settings` Single. Unconfigured — including on
  every fresh site — is a no-op: `status_label()` falls back to the raw internal
  value, never a blank or a Pacific-branded default. `_serialize`/
  `_serialize_many` carry the resolved label as `statusLabel` alongside the raw
  `status`; `sales.inquiry_api`/`quotation_api`'s quoting-rejection message,
  `purchase.reorder_api`'s reorder-suggestion reasons, and
  `reports.warehouse_reports`'s display-replacement reasons all route their
  status-naming text through the same resolver instead of embedding the raw
  value. `get_status_label_map` is the read endpoint for screens that need the
  whole set (filter dropdowns, the item edit form) rather than one resolved
  label already riding on a loaded product.
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

## Colour and Base Colour (BRD C.1.2)

"Colour is captured as both Colour and Base Colour." `custom_color` is **Colour** -- the item's own colour,
the one the item name is composed from (series + colour + finish, client requirement 8.5). It used to be
labelled "Base Colour" in Desk; it was empty on every migrated item, so the relabel moved no data.
`custom_base_colour` is **Base Colour**, the broad family for filtering; its picker
(`list_item_base_colours`) falls back to `COLOR_DEFAULTS`, which are families. ISS-2026-00632's
`custom_colour_attribute_1/2` stay as they were.


## Top Dealers (BRD C.10.4), sample_api.top_dealers

"Top dealers for sample/display planning are identified from retail sales over the last six
months, excluding bulk/project sales." Pacific, 26 Oct 2026: ranked by the invoices already in
the system -- the migrated Tally `Sales Invoice` records -- not this app's own Sales Orders,
which don't yet cover six months of history. Those carry no retail/bulk tag, so bulk/project is
excluded by the dealer's own `custom_dealer_type` (Bulk / Project), the same field BRD C.1.4's
dealer type uses; once an invoice posted from this app carries `custom_order_channel` (see the
billing work, `sales/README.md`), that's checked too. Credit notes, opening entries and
proformas (`invoice_api._type_condition`) don't count -- only real sales. An optional `item`
narrows it to dealers who bought that item, to decide whose display it should be.


## UOM conversion and tile dimensions (sheet 7.8, 26 Oct 2026), catalog.utils

Order/quotation lines no longer have to be entered in boxes. `ORDER_UOMS` (Box/Pcs/Sqft/Sqm/
Kg/Gram) and `convert_qty_to_boxes(item, qty, uom)` are the single conversion point, called once
by `sales.order_api.create_order` and `sales.quotation_api.create_quotation` -- before
`auto_classify_channel`, the credit-limit estimate, or pricing ever see `qty`, since every one of
those is a box-qty rule. Conversion uses the item's own existing per-box figures
(`custom_pieces_per_box`/`custom_sqft_per_box`/`custom_weight_per_box_kg`; Sqm derives from Sqft
via `SQFT_TO_SQM`, Gram from the Kg figure); an item missing the figure a given unit needs throws
a clear `ValidationError` naming it, rather than guessing. Whatever doesn't divide evenly into a
whole box rounds **up** (`math.ceil`) -- a dealer can end up with slightly more tile than asked
for, never less, since a box can't be physically split.

`normalize_order_line_uoms(lines)` runs the conversion over a whole `lines` payload and tags each
line with `entered_qty`/`entered_uom` *only* when a non-Box unit was actually used (left out
entirely for a plain box order) -- these land on `custom_entered_qty`/`custom_entered_uom`
(Sales Order Item / Quotation Item) purely for display, e.g. "entered as 50 Sqft" next to the
real box qty. `qty` itself is always the box-equivalent; nothing downstream (stock, picking,
invoicing) ever needs to know what unit a line was entered in.

`custom_height_mm`/`custom_width_mm` (Item, Float) capture the physical tile dimensions.
`derive_sqft_per_box(height_mm, width_mm, pieces_per_box)` is used once, at `create_product`
time, to fill in `custom_sqft_per_box` automatically when it's left blank and the three inputs
are all known -- never overrides an explicitly supplied value, and never retrofits existing
items. No Series-level mirroring yet (unlike pieces/sqft/weight per box) -- a deliberate v1 scope
cut; items created from a Series without their own height/width still need `custom_sqft_per_box`
set directly, same as before this existed.
