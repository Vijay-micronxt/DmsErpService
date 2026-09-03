# dms_erp — DMS

Custom Frappe app backing Pacific Inc's internal ops system (installed into an
existing ERPNext site). Serves the `pacific-tileflow` React/TanStack SPA over a
pure JSON API — no Frappe Desk, no cookies, no redirects.

## Status: Phase 0 (staff auth) + Phase 2 (product / pricing / dealer catalog) + Phase 3 (warehouse) + Phase 4 (purchase orders / reorder engine) + Phase 5 (inquiries / quotations / orders / picking) + Phase 6 (damage/insurance claims, unloading payment) + Phase 7 (WhatsApp / comms) + Phase 8 (dashboard / analytics)

All eight phases are now implemented: `auth`, `catalog`, `pricing`,
`warehouse`, `purchase`, `sales`, `finance`, `comms`, and a new `dashboard`
module for Phase 8 (like `sales` in Phase 5, Dashboard/Analytics didn't map
onto any module Phase 0 bootstrapped, so it gets its own).

Phase 8 has no doctype at all — it's pure read/aggregation over everything
the other seven phases built. Most of the frontend's own `dashboard.tsx` is
hardcoded demo numbers rather than real KPIs; this phase computes the real
thing everywhere the data now exists to support it (which, after seven
phases, is nearly everywhere), and stubs — clearly — only what would need
an accounts-receivable ledger this app was never scoped to post. See
`dms_erp/dashboard/README.md`.

Phase 7 adds a WhatsApp message log (`dms_erp/comms/`) as the system of
record only — the real WhatsApp Business API send/receive integration is
middleware-side per the BRD, so this phase exposes a webhook contract for
that (not-yet-built) middleware rather than calling WhatsApp itself. See
`dms_erp/comms/README.md`.

Phase 6 adds Insurance Claims and Unloading Charges (`dms_erp/finance/`) as
custom doctypes linked back into Phase 3's Stock Entry/Inward Truck — neither
posts to the General Ledger, since that needs Chart-of-Accounts accounts this
app can't assume exist on the target site. See `dms_erp/finance/README.md`.

Phase 5 adds a `sales` module — not part of the module list Phase 0
bootstrapped, since that plan didn't allocate one for this phase. It supplies
the demand data Phase 4's reorder engine was stubbing (missed demand, pending
inquiries) — a natural follow-up worth wiring in once this settles, though not
done automatically as part of this phase. See `dms_erp/sales/README.md`.

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

Phase 7's webhook endpoints need their own shared secret (a placeholder until
the real WhatsApp middleware integration exists — see `dms_erp/comms/README.md`):

```bash
bench --site <site-name> set-config dms_erp_whatsapp_webhook_secret "<a long random secret>"
```

## Roles

Four Frappe Roles are created automatically on install/migrate, all with
`desk_access = 0` (staff users never need `/app`):

| Frontend role | Frappe Role         |
|---------------|----------------------|
| sales         | `DMS Sales`      |
| warehouse     | `DMS Warehouse`  |
| purchase      | `DMS Purchase`   |
| management    | `DMS Management` |

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
    "roles": ["DMS Warehouse"],
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
restricted to `DMS Purchase` / `DMS Management` (or `System Manager`);
everyone can read.

| Method | Notes |
|---|---|
| `dms_erp.catalog.api.list_products` | paginated (`limit`/`offset`, `{items, total}`); optional `dealer` param filters to that dealer's catalog (BRD §6.4); optional `search` |
| `dms_erp.catalog.api.get_product` | `item` (Item Code) |
| `dms_erp.catalog.api.create_product` | write-restricted; also seeds a Pending `Item Price Proposal` |
| `dms_erp.catalog.api.update_product` | write-restricted; `patch` is a partial `Product`-shaped dict |
| `dms_erp.catalog.api.list_item_groups` | paginated; optional `search`; leaf `Item Group` categories only |
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
restricted to `DMS Warehouse` / `DMS Management` (or `System
Manager`); inward-truck writes also allow `DMS Purchase`. Everyone can
read.

| Method | Notes |
|---|---|
| `bay_api.list_warehouse_groups` | the physical warehouses `create_bay`'s `parent_warehouse` needs — its `id` is the ERPNext-autonamed raw name, not the clean display name |
| `bay_api.create_warehouse_group` | write-restricted; adds another physical warehouse — the seeded two aren't a hard limit |
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
to `DMS Purchase` / `DMS Management` (or `System Manager`); everyone
can read.

| Method | Notes |
|---|---|
| `po_api.list_purchase_orders` / `get_purchase_order` | native Purchase Order, submitted on creation |
| `po_api.create_purchase_order` | write-restricted; single item line, matching the frontend's "Raise PO" flow |
| `po_api.set_line_ready` | write-restricted; clamps to `[0, orderedQty]` |
| `po_api.line_progress` | `plannedQty` from linked Inward Trucks, `receivedQty` from ERPNext's native `received_qty` |
| `reorder_api.reorder_suggestions` | read-only; real stock/safety-stock signal today, zeroed demand signals pending Phase 5 |

## Sales API (Phase 5)

All under `/api/method/dms_erp.sales.<file>.<method>`, all requiring
`Authorization: Bearer <access_token>`. Inquiry/Quotation/Order writes are
restricted to `DMS Sales` / `DMS Management` (or `System Manager`);
Pick Task writes to `DMS Warehouse` / `DMS Management` (or `System
Manager`). Everyone reads.

| Method | Notes |
|---|---|
| `inquiry_api.list_inquiries` / `get_inquiry` | paginated (`limit`/`offset`, `{items, total}`); optional `dealer`/`status`/`search` filters |
| `inquiry_api.create_inquiry` | write-restricted; always starts `Open` |
| `inquiry_api.update_inquiry` | write-restricted; `patch` is a partial `Inquiry`-shaped dict |
| `quotation_api.list_quotations` / `get_quotation` | native Quotation; paginated, optional `dealer`/`search` filters |
| `quotation_api.create_quotation` | write-restricted; rejects items outside the dealer's catalog or with no approved price; rate = approved dealer price × (1 + markup%) |
| `quotation_api.convert_to_order` | write-restricted; wraps ERPNext's native Quotation→Sales Order mapper |
| `order_api.list_orders` / `get_order` | native Sales Order; paginated, optional `dealer`/`stage`/`search` filters |
| `order_api.create_order` | write-restricted; direct Inquiry→Order (no markup) — the only other path is `convert_to_order` |
| `order_api.advance_order_stage` | write-restricted; only the next forward stage or `Cancelled` (not after `Delivered`); entering `Picking` auto-creates Pick Tasks |
| `picking_api.list_pick_tasks` | optional `order` filter |
| `picking_api.auto_allocate` | write-restricted; allocates `min(qty, available)` from live stock across non-blocked bays |
| `picking_api.patch_task` | write-restricted; assign picker / adjust status / batch / bay |

## Finance API (Phase 6)

All under `/api/method/dms_erp.finance.<file>.<method>`, all requiring
`Authorization: Bearer <access_token>`. Writes restricted to `Pacific
Warehouse` / `DMS Management` (or `System Manager`); everyone reads.

| Method | Notes |
|---|---|
| `claims_api.list_claims` / `get_claim` | optional `status` filter |
| `claims_api.file_claim` | write-restricted; `stock_entry` must be a Damage→Insurance Claim transfer, one claim per transfer; writes back to that Stock Entry's `custom_claim_ref` |
| `claims_api.update_claim_status` | write-restricted; `Settled` stamps `settledAmount`/`settledAt` |
| `claims_api.claim_summary` | receivable (Filed+Approved) / settled / rejected-count totals |
| `unloading_api.list_charges` | optional `status` filter |
| `unloading_api.get_charge_for_truck` | `null` if none recorded yet |
| `unloading_api.record_charge` | write-restricted; one per Inward Truck; `boxes` read from the truck, `chargeAmount` computed on read |
| `unloading_api.mark_paid` | write-restricted; stamps `paidBy`/`paidAt` from the authenticated caller |

## Comms API (Phase 7)

All under `/api/method/dms_erp.comms.api.<method>`. Most require
`Authorization: Bearer <access_token>` (writes restricted to `DMS Sales` /
`DMS Management` / `System Manager`, reads open to everyone); the two
`webhook_*` methods are `allow_guest` instead, gated by
`dms_erp_whatsapp_webhook_secret`.

| Method | Auth | Notes |
|---|---|---|
| `list_messages` | staff | full thread for a dealer, ascending by `sentAt` |
| `last_message` | staff | most recent message, or `null` |
| `unreplied_inbound_count` | staff | `0` or `1` — has staff replied since the dealer's last inbound message |
| `list_templates` | staff | static canned quick-replies with `{placeholder}` slots |
| `send_message` | staff, write-restricted | creates an Outbound message, status `Sent` |
| `mark_read` | staff, write-restricted | Inbound message → `Read` |
| `webhook_inbound_message` | shared secret | middleware calls this when a dealer sends a message; creates Inbound, status `Delivered` |
| `webhook_status_update` | shared secret | middleware calls this with a real delivery receipt for an Outbound message |

## Dashboard API (Phase 8)

All under `/api/method/dms_erp.dashboard.api.<method>`, requiring
`Authorization: Bearer <access_token>`. Each is gated to its own role (or
Management, who can see all four) — there is no write access, this module
only reads.

| Method | Role | Notes |
|---|---|---|
| `sales_dashboard` | Sales, Management | today's inquiries, pending quotations, orders this month, missed-demand value, 30-day inquiry trend, actionable inquiries |
| `warehouse_dashboard` | Warehouse, Management | ported `warehouseKpis`/`warehouseAlerts` from the frontend, now over live data, plus today's incoming trucks |
| `purchase_dashboard` | Purchase, Management | pending/delayed POs, this week's pickups, reorder-suggestion count, monthly purchase trend, materials ready for pickup |
| `management_dashboard` | Management only | sales MTD, claimable insurance value, top moving item, sales by dealer, credit-exposure alerts |

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
  "DMS" — deliberately generic (not tied to any one distributor's name) so the
  app stays white-labelable; renamed from the original client-specific
  "Pacific DMS" title and "Pacific"-prefixed field labels/doctype/roles.
- **License**: `hooks.py` sets `Proprietary`; change if you want something
  else on record.
- **Login is restricted** to users holding one of the four DMS roles (or
  System Manager) — an ERPNext accounting/sales user with no DMS role
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

**Phase 5 additions:**

- **A new `sales` module** was added, not part of Phase 0's original module
  list — that plan didn't allocate one for Inquiries/Quotations/Orders/
  Picking. `dms_erp/modules.txt` now includes `Sales`.
- **The frontend's Inquiry data is currently read-only** (no create/update
  wired up in `pacific-tileflow` yet, unlike every other domain this app has
  backed so far) — this phase still builds the real, mutable backend the BRD
  describes, matching how Phase 0 built real auth against a frontend that
  only had a trivial mock sign-in. The frontend needs its own follow-up work
  to actually call `inquiry_api`.
- **`create_order` requires an Inquiry** — there is no third, source-less way
  to create an Order; the only two paths are direct Inquiry→Order (no markup,
  this endpoint) and Quotation→Order (`quotation_api.convert_to_order`),
  matching the frontend's `sourceType: "Inquiry" | "Quotation"` exactly.
- **Reorder engine not yet wired to real Inquiry/Order data** — Phase 4's
  `reorder_api.py` still returns `0` for `missedDemandQty`/`pendingInquiryQty`/
  `recentRetailSalesQty` even though Phase 5 now has that data available.
  Wiring it up is a natural next step but wasn't done as a side effect of
  this phase — flag if you'd like that pulled in now instead of later.
- **Picking does not move stock** — `auto_allocate`/`patch_task` are
  reservation bookkeeping only; no Delivery Note is created and Bin/Stock
  Ledger Entry are untouched. Matches the frontend, which shows no evidence
  of a real dispatch/stock-out step either.
- **Quotation freight is a plain field**, not wired into ERPNext's native
  Sales Taxes and Charges — that needs a GL account this app can't assume
  exists on every site's chart of accounts.

**Phase 6 additions:**

- **No General Ledger postings** — Insurance Claim and Unloading Charge track
  receivable/settled/paid amounts as their own fields, not via Journal Entry
  or Payment Entry, since both need Chart-of-Accounts accounts this app can't
  assume exist on the target site (same reasoning as Quotation freight
  above). Flag if you'd rather this app assume/create specific GL accounts
  so real postings can happen instead.
- **Insurance claim write access is Warehouse/Management**, not Purchase —
  a judgment call, since the BRD doesn't explicitly assign an actor for
  filing/settling claims the way it does for pricing approval or catalog
  assignment. Warehouse identifies the damage and escalates it physically
  (Phase 3's transfer), so gating the financial follow-up the same way
  seemed the more consistent default. Flag if this should sit with Purchase
  or a future Finance role instead.
- **`insurer` is a plain text field**, not a link to any ERPNext party
  doctype — Supplier/Customer don't fit "insurance company" without misusing
  those entities for something they don't mean.

**Phase 7 additions:**

- **No real WhatsApp Business API integration** — sending/receiving actual
  WhatsApp messages is explicitly middleware-side per the BRD, and that
  middleware doesn't exist yet. This phase is the system of record and a
  webhook contract for it, not a Meta/Twilio integration. Flag if you'd
  rather this app called a WhatsApp provider directly instead of waiting on
  external middleware.
- **Webhook auth is a placeholder shared secret**, not a real verification
  scheme — there's no spec yet for what the actual middleware will use
  (Meta's verify-token handshake, HMAC signatures, an IP allowlist). Treat
  `dms_erp_whatsapp_webhook_secret` as something to replace, not the final
  design.
- **Message templates are static**, matching the frontend — not a doctype,
  since nothing here needs them editable without a deploy yet. Flag if
  Sales should be able to edit them without engineering involvement.
- **Considered and rejected**: extending Frappe's native `Communication`
  doctype instead of a new one. Its `status` field means something different
  (thread-handling state, not delivery receipt) and "WhatsApp" isn't a stock
  `communication_medium` — reusing it would mean customizing a doctype other
  unrelated core features also share. See `dms_erp/comms/README.md`.

**Phase 8 additions:**

- **`outstandingReceivables` is a hard `0`**, and the management dashboard's
  credit alert compares order value (not true receivables) against
  `Customer.credit_limit` — this app has never posted a Sales Invoice, so
  real AR-aging data doesn't exist. Flag if that should become its own
  phase rather than staying stubbed.
- **"Pending Quotations" uses Quotation's native `status = "Open"`** as the
  closest proxy for "awaiting dealer reply" — ERPNext's own Quotation status
  vocabulary doesn't have a single field that means exactly that.
- **"Top Moving Item"** is a genuine judgment call: the frontend hardcodes
  `"PVT-6060"` with no algorithm behind it. This app ranks by total qty sold
  across submitted Sales Orders (all-time) and reports current stock
  alongside it — a reasonable, real interpretation of "moving," but not
  something the frontend specified.
- **Trend windows differ slightly from the frontend's mock arrays**: the
  inquiry trend is a real 30-day daily count, the purchase trend a real
  6-month-by-calendar-month sum — chosen to be genuinely useful now that
  they're real queries, not because the frontend's mock windows meant
  anything precise (they were fixed-length arrays of made-up numbers).
