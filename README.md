# dms_erp — Pacific DMS

Custom Frappe app backing Pacific Inc's internal ops system (installed into an
existing ERPNext site). Serves the `pacific-tileflow` React/TanStack SPA over a
pure JSON API — no Frappe Desk, no cookies, no redirects.

## Status: Phase 0 (staff auth) + Phase 2 (product / pricing / dealer catalog) + Phase 3 (warehouse) + Phase 4 (purchase orders / reorder engine)

`auth`, `catalog`, `pricing`, `warehouse` and `purchase` are implemented.
`finance`, `comms` are still empty placeholders (see each module's
README.md) reserved for later phases so they drop in without restructuring.

Phase 4 builds Purchase Orders on ERPNext's native `Purchase Order` doctype
and closes the loop Phase 3 deliberately left open: Purchase Receipts posted
by Bay Allocation now link back to the PO line they fulfill (so ERPNext's own
`received_qty` tracks correctly) and prefer the PO's negotiated rate over the
Phase 2 price-proposal stand-in. The reorder-suggestion engine is real for
the signals available today (current stock, safety stock, non-reorderable
status) and honestly stubs the rest (missed demand, pending inquiries, sales
velocity) until Phase 5 supplies that data. See `dms_erp/purchase/README.md`.

Phase 3 builds bays, allocation, inward and transfers entirely on ERPNext's
native stock doctypes (`Warehouse`, `Stock Ledger Entry`/`Bin`, `Purchase
Receipt`, `Stock Entry`, `Batch`) per the architecture decision — there is no
custom stock ledger anywhere in this app. See `dms_erp/warehouse/README.md`
for the mapping.

Phase 2 leans on ERPNext's native doctypes wherever one already exists —
`Item`, `Item Group`, `Item Price`/`Price List`, `Item Alternative` — adding
custom fields/doctypes only where ERPNext genuinely has no equivalent
(discontinuation lifecycle + a few tile-specific attributes on Item; the
Item Price Proposal approval workflow; per-dealer catalog visibility). See
`dms_erp/catalog/README.md` and `dms_erp/pricing/README.md` for the mapping.

## Install

This container doesn't have `bench` installed, so the app was hand-scaffolded
to match what `bench new-app` would produce. To install it for real:

```bash
bench get-app dms_erp /path/to/this/repo   # or a git URL
bench --site <site-name> install-app dms_erp
bench --site <site-name> migrate           # creates doctypes, custom fields, roles, Item Groups, Dealer price list
```

From Phase 2 onward this app requires `erpnext` to already be installed on the
target site (`required_apps` in `hooks.py` now includes it) — catalog/pricing
build directly on ERPNext's native Item/Item Price/Price List doctypes.

Phase 3's warehouse setup (physical warehouses, bay custom fields) needs a
default Company configured on the site — if none exists yet at install/migrate
time it logs a warning and skips warehouse creation rather than failing the
whole migration; re-run `bench --site <site-name> migrate` once a Company
exists.

## Required site_config.json keys

JWT signing keys are **not** auto-generated — set them explicitly so a fresh
`bench new-site` doesn't silently mint a throwaway secret:

```bash
bench --site <site-name> set-config dms_erp_jwt_keys '{"k1": "<a long random secret>"}' --parse
bench --site <site-name> set-config dms_erp_jwt_active_kid k1
```

Optional (defaults shown):

```json
{
  "dms_erp_access_token_ttl": 2400,
  "dms_erp_refresh_token_ttl_days": 30
}
```

**Key rotation:** add a new kid to `dms_erp_jwt_keys`, point
`dms_erp_jwt_active_kid` at it. Keep the old kid's secret in the map until every
access token signed with it has expired (`dms_erp_access_token_ttl` seconds
after the rotation), then remove it.

## Roles

Four Frappe Roles are created automatically on install/migrate, all with
`desk_access = 0` (staff users never need `/app`):

| Frontend role | Frappe Role         |
|---------------|----------------------|
| sales         | `Pacific Sales`      |
| warehouse     | `Pacific Warehouse`  |
| purchase      | `Pacific Purchase`   |
| management    | `Pacific Management` |

`System Manager` is also accepted as a login-time escape hatch for admin
accounts, but every real staff user should be assigned one of the four roles
above.

## API

All endpoints are under `/api/method/dms_erp.auth.api.<method>`. `login` and
`refresh_token` are the only unauthenticated (`allow_guest`) endpoints;
everything else requires `Authorization: Bearer <access_token>`.

| Method | Auth required | Body / params |
|---|---|---|
| `login` | no | `usr`, `pwd`, `device_id`, `device_name?` |
| `refresh_token` | no | `refresh_token` |
| `logout` | no | `refresh_token` |
| `logout_all` | yes | — |
| `me` | yes | — |

`login` and the successful path of `refresh_token` return:

```json
{
  "access_token": "<jwt>",
  "refresh_token": "<opaque>",
  "token_type": "Bearer",
  "expires_in": 2400,
  "user": {
    "name": "jane@pacific.example",
    "email": "jane@pacific.example",
    "full_name": "Jane Doe",
    "roles": ["Pacific Warehouse"],
    "app_roles": ["warehouse"],
    "primary_role": "warehouse"
  }
}
```

(`user` is omitted from `refresh_token`'s response — call `me` if the client
needs a fresh profile.)

Example:

```bash
curl -X POST https://<site>/api/method/dms_erp.auth.api.login \
  -H 'Content-Type: application/json' \
  -d '{"usr":"jane@pacific.example","pwd":"...","device_id":"pos-terminal-1","device_name":"Warehouse iPad"}'

curl https://<site>/api/method/dms_erp.auth.api.me \
  -H 'Authorization: Bearer <access_token>'
```

### Refresh token rotation & reuse detection

Every call to `refresh_token` consumes the presented token and issues a new
one. If a refresh token that was already rotated out gets presented again
(replay of a stolen token), the session is revoked immediately rather than
issued new tokens.

## Catalog & pricing API (Phase 2)

All under `/api/method/dms_erp.<module>.<file>.<method>`, all requiring
`Authorization: Bearer <access_token>`. Product-master and pricing writes are
restricted to `Pacific Purchase` / `Pacific Management` (or `System Manager`);
everyone can read.

| Method | Notes |
|---|---|
| `dms_erp.catalog.api.list_products` | optional `dealer` param filters to that dealer's catalog (BRD §6.4) |
| `dms_erp.catalog.api.get_product` | `item` (Item Code) |
| `dms_erp.catalog.api.create_product` | write-restricted; also seeds a Pending `Item Price Proposal` |
| `dms_erp.catalog.api.update_product` | write-restricted; `patch` is a partial `Product`-shaped dict |
| `dms_erp.catalog.dealer_catalog_api.catalog_for` | item codes visible to a dealer; full catalog if unassigned |
| `dms_erp.catalog.dealer_catalog_api.is_visible` | single dealer+item check |
| `dms_erp.catalog.dealer_catalog_api.set_product_visibility` | write-restricted |
| `dms_erp.catalog.dealer_catalog_api.set_category_visibility` | write-restricted; bulk by Item Group |
| `dms_erp.catalog.dealer_catalog_api.category_coverage` | `{total, visible}` for a dealer + Item Group |
| `dms_erp.pricing.api.list_price_records` / `get_price_record` | landing cost + suggested price computed on the fly, never stored |
| `dms_erp.pricing.api.save_cost_inputs` | write-restricted |
| `dms_erp.pricing.api.approve_price` | write-restricted; publishes to the native `Item Price` (Dealer price list) and appends an audit-trail row; `approved_by` is always the authenticated caller, never client-supplied |

Product responses match the frontend's `Product` shape (camelCase) field-for-field,
with `stockQty`/`bay`/`lastSoldDays` stubbed (`0`/`"—"`/`0`) pending Phase 3/5.
`dealerPrice` is `null` until a proposal is approved — the item-master form no
longer sets a price directly, unlike the current frontend mock (see
`dms_erp/catalog/README.md` for why).

## Warehouse API (Phase 3)

All under `/api/method/dms_erp.warehouse.<file>.<method>`, all requiring
`Authorization: Bearer <access_token>`. Bay/allocation/transfer writes are
restricted to `Pacific Warehouse` / `Pacific Management` (or `System
Manager`); inward-truck writes also allow `Pacific Purchase`. Everyone can
read.

| Method | Notes |
|---|---|
| `bay_api.list_bays` / `get_bay_detail` | includes live occupancy (`occupiedBoxes`/`occupancyPct`/`freeBoxes`) |
| `bay_api.create_bay` / `create_bay_grid` / `update_bay` / `delete_bay` | write-restricted; delete relies on ERPNext's own refusal to delete a Warehouse with stock |
| `stock_api.list_stock` | optional `bay`/`item` filters; live aggregate over Stock Ledger Entry, not a stored table |
| `stock_api.suggest_bays` | category/free-capacity scoring, ported from the frontend's `suggestBays` |
| `stock_api.validate_allocation` | error/warning issues for a proposed bay+qty+category, ported from `validateAllocation` |
| `inward_api.list_trucks` / `add_truck` / `advance_truck` | truck gate queue; write-restricted |
| `allocation_api.create_allocation` | write-restricted; posts and submits a native Purchase Receipt — the actual stock-effecting event |
| `allocation_api.mark_allocation_printed` / `confirm_putaway` | write-restricted; `confirm_putaway` does not move stock again |
| `allocation_api.resolve_scan` | read-only; same `PI-BAY|`/`PI-ITEM|` code format as the frontend, plus bare-code lookup |
| `transfer_api.list_transfers` / `transfer_stock` | native Stock Entry (Material Transfer); write-restricted |

## Purchase API (Phase 4)

All under `/api/method/dms_erp.purchase.<file>.<method>`, all requiring
`Authorization: Bearer <access_token>`. Purchase Order writes are restricted
to `Pacific Purchase` / `Pacific Management` (or `System Manager`); everyone
can read.

| Method | Notes |
|---|---|
| `po_api.list_purchase_orders` / `get_purchase_order` | native Purchase Order, submitted on creation |
| `po_api.create_purchase_order` | write-restricted; single item line, matching the frontend's "Raise PO" flow |
| `po_api.set_line_ready` | write-restricted; clamps to `[0, orderedQty]` |
| `po_api.line_progress` | `plannedQty` from linked Inward Trucks, `receivedQty` from ERPNext's native `received_qty` |
| `reorder_api.reorder_suggestions` | read-only; real stock/safety-stock signal today, zeroed demand signals pending Phase 5 |

## Assumptions made (please confirm/correct)

Since this repo started blank with no bench/mariadb/frappe available in this
container, the following were assumed and are worth confirming:

- **Frappe/ERPNext version**: targeting a recent v15-line site (uses
  `pyproject.toml`-style app layout, not the older `setup.py` one). If you're
  on v13/v14, the app still works but you may want a `setup.py` alongside.
- **Environment**: plain `bench` (local or Docker-based `frappe_docker`), not
  Frappe Cloud — nothing here is Frappe-Cloud-specific either way.
- **Target site name**: not pinned down anywhere in the app; you supply it at
  `bench --site <site> install-app` time.
- **App name**: `dms_erp` (derived from the repo name `DmsErpService`), title
  "Pacific DMS". Renaming later is a bigger diff (module paths, doctype
  `module` field) than fixing it now if it's wrong.
- **License**: `hooks.py` sets `Proprietary`; change if you want something
  else on record.
- **Login is restricted** to users holding one of the four Pacific roles (or
  System Manager) — an ERPNext accounting/sales user with no Pacific role
  cannot log into the staff app even with valid Frappe credentials. Flag if
  you wanted this open to any enabled User instead.

**Phase 2 additions:**

- **Stock UOM**: new items default to `Box` as `stock_uom` (matches how the
  warehouse module already talks about stock in boxes). `piecesPerBox` /
  `sqftPerBox` / `weightPerBoxKg` are plain custom fields, not modeled as
  ERPNext UOM conversions yet — revisit if Phase 3 needs real UOM math (e.g.
  transacting in sqft) rather than just displaying these numbers.
- **Dealer = Customer**: dealer-catalog visibility links to ERPNext's native
  `Customer` doctype by name. Pacific-specific dealer attributes from the
  frontend's `Dealer` type (GST, credit limit, salesperson, Retail/Project/
  Sub-dealer type) mostly have native ERPNext equivalents already (tax ID,
  credit limit, Sales Team) or weren't needed for this phase — full dealer
  master fields weren't built now since nothing in Phase 2 reads them; flag
  if you want that pulled forward instead of left for Phase 5.
- **Pricing approval is gated** to Purchase/Management — the item-master
  create/update endpoints and dealer-catalog writes are gated the same way,
  per the BRD's actor assignments in `pacific-tileflow/docs/business-flow.md`.
  Sales/Warehouse get read-only access to all of it.
- **`dealerPrice` is `null` until approved** — a deliberate difference from
  the current frontend mock, which sets a price immediately on item creation.
  A real approval gate means "priced but not yet approved" has to be a
  representable state; the frontend will need a small update once wired to
  this API to render that (e.g. "Pending price" instead of a number).

**Phase 3 additions:**

- **Items are now batch-tracked** (`has_batch_no=1`) — a correction to Phase
  2's `create_product`, discovered once it was clear every BayLot carries a
  batch number. `warehouse/setup.py` also flips this on for any pre-existing
  Pacific items that predate this change.
- **Purchase Receipt rate** on allocation-confirm comes from the item's
  `Item Price Proposal.purchaseCost` (Phase 2), defaulting to 0 with no hard
  failure if none exists yet. Phase 4's real PO will have its own negotiated
  rate per line — this is a reasonable stand-in until then, not the final
  source of truth.
- **PR-to-PO linking deferred to Phase 4** — `create_allocation` posts a
  standalone Purchase Receipt (supplier + items only); it does not yet set
  `purchase_order`/`purchase_order_item` on the PR items, so PO received-qty
  won't track automatically yet even though `Inward Truck` already carries
  those links informationally.
- **Bay codes vs. Warehouse doc names**: ERPNext auto-suffixes Warehouse's
  `name` with the company abbreviation (e.g. "Main Bay A-01 - PTC"). Every
  API in this module takes/returns the clean human `code` (`custom_bay_code`,
  e.g. "A-01") and resolves internally — never assume `Bay.id` looks like the
  code.
- **Transfer/receipt reference numbers** are ERPNext's own native IDs (e.g.
  "MAT-STE-2026-00001", "MAT-PRE-2026-00001"), not the frontend mock's
  cosmetic "BTR-2408-001" format — Stock Entry/Purchase Receipt naming isn't
  something this app overrides.

**Phase 4 additions:**

- **POs submit immediately** on creation — there's no draft/approval step,
  matching the frontend's one-action "Raise PO". If a real approval workflow
  is wanted later, that's a bigger change (Frappe's Workflow doctype, or a
  held-as-draft PO with a separate submit endpoint).
- **Reorder suggestions include every Item**, not just Pacific's four seeded
  categories — reasonable today since this is the only Item Master phase has
  built, but worth revisiting if the target site has non-Pacific items too.
- **`missedDemandQty`/`pendingInquiryQty`/`recentRetailSalesQty` are `0`**
  until Phase 5 exists — real zeros in the sense of "not computed", not a
  claim that retail demand is actually zero. `reorder_api.py`'s docstring
  spells out exactly what to wire up once Inquiry/Order doctypes land.
