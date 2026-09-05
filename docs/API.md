# DMS — API Reference

Generated from the whitelisted methods across `dms_erp`'s modules, on `main` (PRs #1–#25 merged, through Phase 20 — the full 20-report BRD "Reports and Dashboards" set). Every response shape is read directly from each module's own `_serialize`/return value.

This file is the source-of-truth companion to the interactive API Reference artifact shared with the frontend team — the endpoints, params, and shapes below should always match. If they diverge, this file (committed alongside the code that defines the endpoints) is the one to trust.

## Calling convention

- **Base URL**: `https://<site>/api/method/<dotted.path>`
- **GET requests**: params as querystring — `?dealer=CUST-0004&status=Open`
- **POST / PUT requests**: params as JSON body
- **Auth header**: `Authorization: Bearer <access_token>` on every call except the endpoints marked `guest`
- **No cookies**: the backend never reads a Desk session (`sid`) cookie — Bearer is the only credential, so there's nothing CSRF-related to send either

```
POST /api/method/dms_erp.auth.api.login
{ "usr": "priya@pacific.example", "pwd": "********", "device_id": "web-chrome-8f2c" }
→ { "access_token": "eyJ...", "refresh_token": "8Kx...", "expires_in": 3600, "user": {...} }

// every request:
GET /api/method/dms_erp.sales.dealer_api.list_dealers
Authorization: Bearer eyJ...

// access token expired (401) → refresh, then retry the original call:
POST /api/method/dms_erp.auth.api.refresh_token
{ "refresh_token": "8Kx..." }
→ { "access_token": "eyJ...(new)", "refresh_token": "9Lz...(rotated, discard the old one)", "expires_in": 3600 }
```

**Status legend**: `GET` read · `POST` create/action · `PUT` update (accepted alongside POST) · `DELETE` remove · **PARTIAL** exists, shape/behavior differs from what the frontend expects · **NOT BUILT** no endpoint yet.

## Cross-cutting flags

1. **Inquiry + Quotation persistence** — the backend leads on both. `Inquiry` is a real custom doctype with full CRUD; `Quotation` is native ERPNext with create, line-edit, and status control.
2. **No GL account is ever hardcoded or guessed.** Claim settlement and unloading payment both route through **DMS Accounting Settings** — a single on/off flag plus five nullable Account links. While it's off (the default), both actions are pure status/amount updates with zero accounting side effect.

## Modules

- [Auth](#auth)
- [Dealers](#dealers)
- [Products / Item Master](#products-item-master)
- [Pricing](#pricing)
- [Dealer Catalog Visibility](#dealer-catalog-visibility)
- [Inquiries](#inquiries)
- [Quotations](#quotations)
- [Orders](#orders)
- [WhatsApp / Communications](#whatsapp-communications)
- [Warehouse — Bay Master](#warehouse-bay-master)
- [Warehouse — Stock / Lots](#warehouse-stock-lots)
- [Warehouse — Transfers](#warehouse-transfers)
- [Warehouse — Scan](#warehouse-scan)
- [Inward](#inward)
- [Picking](#picking)
- [Purchase Orders](#purchase-orders)
- [Pickup Run](#pickup-run)
- [Purchase Requirements / Reorder Planning](#purchase-requirements-reorder-planning)
- [Damage & Insurance Claims](#damage-insurance-claims)
- [Unloading Payment](#unloading-payment)
- [Dashboard](#dashboard)
- [Reports — Sales](#reports-sales)
- [Reports — Warehouse](#reports-warehouse)
- [Reports — Purchase](#reports-purchase)
- [Reports — Finance](#reports-finance)
- [Reports — Catalog](#reports-catalog)
- [Reports — Forecasting](#reports-forecasting)

---

## Auth

Foundation — must work before anything else does. Staff username+password only; dealer-facing login (OTP or password) is not built.

> **NOT BUILT** — Dealer OTP-over-WhatsApp (request + verify): Not built. The auth module's own docstring says so explicitly — reserved for a separate dealer-facing app, out of scope for the current staff-app backend.

> **NOT BUILT** — Dealer password login: Not built. `login()` requires one of the four staff roles; a dealer account has none of them, so this isn't a config toggle away — it needs its own path.

#### POST `dms_erp.auth.api.login` · `guest` (no Bearer token required)

**Staff login** — Username+password → JWT access/refresh pair. Requires one of the four DMS roles (System Manager also allowed as an admin escape hatch).

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `usr` | string | required | Frappe user id / email |
| `pwd` | string | required |  |
| `device_id` | string | required | stable per-install id, used to key the Auth Session |
| `device_name` | string | optional | e.g. "iPhone 14 — Priya" |

**Response**

```json
{
  "access_token": "eyJhbGciOi...",
  "refresh_token": "8Kx2m9Qh...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "user": {
    "name": "priya@pacific.example",
    "email": "priya@pacific.example",
    "full_name": "Priya Shah",
    "roles": ["DMS Sales", "..."],
    "app_roles": ["sales"],
    "primary_role": "sales"
  }
}
```

> raises AuthenticationError on bad credentials, disabled account, or a role outside the four DMS roles


#### POST `dms_erp.auth.api.refresh_token` · `guest` (no Bearer token required)

**Refresh token** — Rotates the refresh token; issues a new access token.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `refresh_token` | string | required |  |

**Response**

```json
{
  "access_token": "eyJhbGciOi...(new)",
  "refresh_token": "9Lz7pR2f...(rotated)",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

> ⚠️ reuse detection: presenting an already-rotated-away token revokes that whole session outright (treated as a stolen-token replay), not just a normal rejection


#### POST `dms_erp.auth.api.logout` · `guest` (no Bearer token required)

**Logout (this device)** — Revokes the session tied to the given refresh token.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `refresh_token` | string | required |  |

**Response**

```json
{ "success": true }
```

> idempotent — always 200, even for an already-revoked or unrecognized token, so a client never has to special-case it


#### POST `dms_erp.auth.api.logout_all`

**Logout (all devices)** — Revokes every active session for the calling user.

**Params**

_No parameters._

**Response**

```json
{ "success": true }
```


#### GET `dms_erp.auth.api.me`

**Current user** — Same profile shape as login's `user` field — useful to re-hydrate on app boot from a stored access token.

**Params**

_No parameters._

**Response**

```json
{
  "name": "priya@pacific.example",
  "email": "priya@pacific.example",
  "full_name": "Priya Shah",
  "roles": ["DMS Sales"],
  "app_roles": ["sales"],
  "primary_role": "sales"
}
```


---

## Dealers

Thin read layer over native Customer — every other module already treats a dealer as a bare Customer id with no custom fields.

#### GET `dms_erp.sales.dealer_api.list_dealers`

**List / search dealers**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `search` | string | optional | substring match on dealer name |
| `disabled` | bool | optional, default false | true to list disabled dealers instead of active ones |

**Response**

```json
[
  { "id": "CUST-0004", "name": "Shree Ganesh Tiles", "group": "Retail Dealer",
    "territory": "Saurashtra", "creditLimit": 500000, "disabled": false },
  { "id": "CUST-0007", "name": "Om Sanitary & Tiles", "group": "Retail Dealer",
    "territory": "Kutch", "creditLimit": 250000, "disabled": false }
]
```

> ⚠️ no city/phone/dealer-code fields — Customer has zero custom fields added anywhere in this app


#### GET `dms_erp.sales.dealer_api.get_dealer`

**Get single dealer**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required | Customer id, e.g. CUST-0004 |

**Response**

```json
{ "id": "CUST-0004", "name": "Shree Ganesh Tiles", "group": "Retail Dealer",
  "territory": "Saurashtra", "creditLimit": 500000, "disabled": false }
```


---

## Products / Item Master

Native ERPNext Item. The 5-state discontinuation lifecycle and altItemId are both real, enforced downstream (reorder engine, quotation gate) — not display labels.

#### GET `dms_erp.catalog.api.list_products`

**List / search products** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | optional | if set, filtered through that dealer's catalog assignment AND current sellability (Phase 11) |
| `search` | string | optional | substring match on product name |
| `category` | string | optional | exact match on Item Group, e.g. "Vitrified" |
| `status` | string | optional | exact match on the 5-state discontinuation lifecycle |
| `supplier` | string | optional | exact match on the item's current landing-cost supplier (Item Price Proposal.supplier) — items with no price proposal at all never match |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "PVT-6060", "code": "PVT-6060", "name": "Marbella Beige Vitrified 600x600",
    "size": "600x600mm", "finish": "Glossy", "color": "Beige", "series": "Marbella",
    "category": "Vitrified", "swatch": "#D8C7A8",
    "stockQty": 1840, "bay": "—", "lastSoldDays": 0,
    "dealerPrice": 560, "status": "Active", "isReorderable": true, "isSellable": true,
    "piecesPerBox": 4, "sqftPerBox": 17.44, "weightPerBoxKg": 32,
    "leadTimeDays": 21, "altItemId": null
  }],
  "total": 214, "limit": 20, "offset": 0
}
```

> ⚠️ bay and lastSoldDays are stubs — a real item can span several bays (see Stock/Lots for the per-bay breakdown), and last-sold has no dedicated aggregation yet


#### GET `dms_erp.catalog.api.get_product`

**Get single product**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `item` | string | required | Item code |

**Response**

```json
(same shape as one row of list_products' "items")
```


#### GET `dms_erp.catalog.api.list_item_groups`

**List / search product categories** — paginated. Backed by the native `Item Group`
tree, filtered to the leaf categories `catalog/setup.py` seeds (the root "All Item
Groups" is excluded). No separate detail endpoint — a category has nothing beyond
its name and parent worth a second call for.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `search` | string | optional | substring match on category name |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [
    { "id": "Vitrified", "name": "Vitrified", "parentItemGroup": "All Item Groups" },
    { "id": "Floor Tiles", "name": "Floor Tiles", "parentItemGroup": "All Item Groups" }
  ],
  "total": 4, "limit": 20, "offset": 0
}
```


#### POST `dms_erp.catalog.api.create_product`

**Create product** — Also seeds a Pending price proposal (Purchase/Management must separately call approve_price to publish dealerPrice).

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `code` | string | required |  |
| `name` | string | required |  |
| `category` | string | required | Item Group |
| `supplier` | string | required | for the seeded price proposal |
| `purchase_cost` | number | required |  |
| `margin_pct` | number | required |  |
| `effective_date` | date | required |  |
| `size, finish, color, series, swatch` | string | optional |  |
| `status` | string | default "Active" | one of the 5 lifecycle states |
| `pieces_per_box, sqft_per_box, weight_per_box_kg` | number | default 0 |  |
| `lead_time_days` | int | default 0 |  |
| `alt_item` | string | optional | substitute Item code |

**Response**

```json
(same shape as get_product)
```


#### POST `dms_erp.catalog.api.update_product`

**Update product** — Patch-style. `status` is validated against the 5-state list; `altItemId` writes/clears the native Item Alternative link.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `item` | string | required | Item code |
| `patch` | object | required | keys: name, category, size, finish, color, series, swatch, status, piecesPerBox, sqftPerBox, weightPerBoxKg, leadTimeDays, altItemId |

**Response**

```json
(same shape as get_product)
```

> status: Active → Partially Discontinued → Factory Discontinued → Display Removal Pending → Pulled Back. isReorderable is false from Factory Discontinued on; isSellable is false only at Pulled Back.


---

## Pricing

Item Price Proposal (custom doctype, holds the audit trail) publishes to native Item Price on approval — every other ERPNext read of item pricing sees the standard thing.

#### GET `dms_erp.pricing.api.list_price_records`

**List price records** — paginated. Every Item Price Proposal, regardless of status.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{ "items": [(same shape as get_price_record)], "total": 62, "limit": 20, "offset": 0 }
```


#### GET `dms_erp.pricing.api.get_price_record`

**Get price record** — Landing-cost inputs, computed landingCost/suggestedPrice, and full approval history — all in one call.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `item` | string | required |  |

**Response**

```json
{
  "productId": "PVT-6060", "supplier": "Orient Ceramics",
  "purchaseCost": 380, "freight": 22, "handling": 8, "otherCosts": 5,
  "marginPct": 25, "effectiveDate": "2026-08-01", "status": "Approved",
  "remarks": "Launch pricing", "landingCost": 415, "suggestedPrice": 519,
  "history": [
    { "id": "row-1", "oldPrice": null, "newPrice": 560, "costPrice": 415,
      "marginPct": 25, "effectiveDate": "2026-08-01", "approvedBy": "priya@pacific.example",
      "reason": "Launch", "updatedAt": "2026-08-01 11:02:14" }
  ]
}
```

> ⚠️ price history is this endpoint's `history` array, not a separate call


#### POST `dms_erp.pricing.api.save_cost_inputs`

**Save cost inputs** — Purchase/Management only.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `item` | string | required |  |
| `supplier` | string | required |  |
| `purchase_cost` | number | required |  |
| `freight, handling, other_costs, margin_pct` | number | default 0 |  |
| `effective_date` | date | optional |  |
| `remarks` | string | optional |  |

**Response**

```json
(same shape as get_price_record)
```


#### POST `dms_erp.pricing.api.approve_price`

**Approve price** — Publishes `final_price` to the native Dealer price list (→ Product.dealerPrice everywhere) and appends a history row.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `item` | string | required |  |
| `final_price` | number | required |  |
| `reason` | string | optional |  |

**Response**

```json
(same shape as get_price_record, status now "Approved")
```


---

## Dealer Catalog Visibility

Assignment (is_visible) and sellability are two separate questions, enforced at different points — see the note on catalog_for.

#### GET `dms_erp.catalog.dealer_catalog_api.catalog_for`

**Get catalog for dealer** — Item codes this dealer can inquire/quote for.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |

**Response**

```json
["PVT-6060", "PVT-8080-GL", "WT-3060-MT"]
```

> already filters to currently-sellable items (Phase 11), not just assignment — a Pulled Back item never appears here even if it was assigned visible before being pulled back


#### POST `dms_erp.catalog.dealer_catalog_api.set_product_visibility`

**Set product visibility** — Purchase/Management only.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |
| `item` | string | required |  |
| `visible` | bool | required |  |

**Response**

```json
{ "success": true }
```


#### POST `dms_erp.catalog.dealer_catalog_api.set_category_visibility`

**Set category visibility (bulk)** — Purchase/Management only.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |
| `item_group` | string | required |  |
| `visible` | bool | required |  |

**Response**

```json
{ "success": true }
```


#### GET `dms_erp.catalog.dealer_catalog_api.category_coverage`

**Category coverage**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |
| `item_group` | string | required |  |

**Response**

```json
{ "total": 24, "visible": 18 }
```


---

## Inquiries

Real custom doctype with full CRUD — the frontend's own creation flow is the thing that's a stub, not this.

#### GET `dms_erp.sales.inquiry_api.list_inquiries`

**List / search inquiries** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | optional |  |
| `status` | string | optional | one of the 10 lifecycle states |
| `search` | string | optional | substring match on item code |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "INQ-2026-00042", "number": "INQ-2026-00042", "date": "2026-08-29",
    "dealerId": "CUST-0004", "productId": "PVT-6060", "qty": 60, "status": "Open",
    "source": "WhatsApp", "expectedDelivery": "2026-09-05", "followUpDate": "2026-08-31",
    "assignedTo": "priya@pacific.example", "remarks": null, "whatsappReplied": false
  }],
  "total": 87, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.sales.inquiry_api.get_inquiry`

**Get single inquiry**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `inquiry` | string | required |  |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.sales.inquiry_api.create_inquiry`

**Create inquiry** — Sales/Management only. Status always starts "Open".

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |
| `item` | string | required |  |
| `qty` | number | required |  |
| `source` | string | required | Phone | WhatsApp | Internal | Other |
| `expected_delivery, follow_up_date` | date | optional |  |
| `assigned_to` | string | optional, defaults to caller |  |
| `remarks` | string | optional |  |

**Response**

```json
(same shape as one list row)
```

> enforces the same dealer-catalog visibility + sellability gate Quotation has always had — a hidden or Pulled Back item is rejected here (PermissionError / ValidationError), not just further down the funnel


#### POST `dms_erp.sales.inquiry_api.update_inquiry`

**Update inquiry** — Sales/Management only. Patch-style.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `inquiry` | string | required |  |
| `patch` | object | required | keys: qty, status, source, expectedDelivery, followUpDate, assignedTo, remarks, whatsappReplied |

**Response**

```json
(same shape as one list row)
```


---

## Quotations

Native ERPNext Quotation, submitted immediately (no draft step). Line editing goes through a cancel+amend cycle, not in-place mutation.

#### GET `dms_erp.sales.quotation_api.list_quotations`

**List / search quotations** — paginated. Excludes cancelled (amend-superseded) quotations.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | optional |  |
| `search` | string | optional | substring match on quotation number |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "SQTN-2026-00011", "number": "SQTN-2026-00011", "date": "2026-08-29",
    "dealerId": "CUST-0004", "validTill": "2026-09-05", "markupPct": 12, "freight": 1500,
    "inquiryId": "INQ-2026-00042",
    "lines": [{ "itemCode": "PVT-6060", "qty": 60, "rate": 627 }],
    "total": 37620, "status": "Open"
  }],
  "total": 31, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.sales.quotation_api.get_quotation`

**Get single quotation**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `quotation` | string | required |  |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.sales.quotation_api.create_quotation`

**Create quotation from inquiry** — Sales/Management only. Every line's rate is computed server-side from the approved dealer price + markup — never client-supplied.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |
| `lines` | array | required | [{ item, qty }] |
| `markup_pct` | number | required |  |
| `freight` | number | default 0 |  |
| `validity_days` | int | default 7 |  |
| `inquiry` | string | optional | links back, and flips that Inquiry to "Quoted" |

**Response**

```json
(same shape as one list row)
```

> rejects any line whose item is outside the dealer's catalog (PermissionError) or not currently sellable (ValidationError)


#### POST `dms_erp.sales.quotation_api.add_quotation_line`

**Add line** — Cancels the current submission and resubmits an amended copy — every line's rate is rebuilt, not just the new one.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `quotation` | string | required |  |
| `item` | string | required |  |
| `qty` | number | required |  |

**Response**

```json
(same shape as one list row — note the "id" changes)
```

> the quotation's id changes on every edit, same as amending any other submitted ERPNext document — always use the id the response returns


#### POST `dms_erp.sales.quotation_api.remove_quotation_line`

**Remove line** — Rejects removing the last remaining line.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `quotation` | string | required |  |
| `item` | string | required |  |

**Response**

```json
(same shape as one list row, new id)
```


#### POST `dms_erp.sales.quotation_api.update_quotation_line_qty`

**Update line qty**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `quotation` | string | required |  |
| `item` | string | required |  |
| `qty` | number | required |  |

**Response**

```json
(same shape as one list row, new id)
```


#### POST `dms_erp.sales.quotation_api.update_quotation_status` · **PARTIAL**

**Update status** — Only "Lost" is settable directly, via ERPNext's native declare_enquiry_lost — every other status is a side effect of create/amend/convert_to_order.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `quotation` | string | required |  |
| `status` | string | required | must be "Lost" — anything else raises ValidationError |
| `lost_reasons` | string[] | optional |  |
| `detailed_reason` | string | optional |  |

**Response**

```json
(same shape as one list row, status "Lost")
```

> ⚠️ can only set Lost — not a general status setter


#### POST `dms_erp.sales.quotation_api.convert_to_order`

**Convert to order** — Reuses ERPNext's own Quotation→Sales Order mapper.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `quotation` | string | required |  |
| `expected_dispatch` | date | optional |  |

**Response**

```json
(Order shape — see Orders below)
```


---

## Orders

Native ERPNext Sales Order. Warehouse-fulfillment stages are layered on top via a custom field + stage-history log.

#### GET `dms_erp.sales.order_api.list_orders`

**List / search orders** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | optional |  |
| `stage` | string | optional | Confirmed | Picking | Ready to Dispatch | Dispatched | Delivered | Cancelled |
| `search` | string | optional | substring match on order number |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "SAL-ORD-2026-00033", "number": "SAL-ORD-2026-00033", "date": "2026-08-29",
    "dealerId": "CUST-0004", "sourceType": "Quotation", "sourceRef": "SQTN-2026-00011",
    "lines": [{ "itemCode": "PVT-6060", "qty": 60, "rate": 627 }],
    "total": 37620, "stage": "Confirmed", "expectedDispatch": "2026-09-05",
    "vehicle": null, "owner": "priya@pacific.example",
    "history": [
      { "stage": "Created", "at": "2026-08-29 10:14:02", "by": "priya@pacific.example", "note": "Converted from SQTN-2026-00011" },
      { "stage": "Confirmed", "at": "2026-08-29 10:14:02", "by": "priya@pacific.example", "note": null }
    ]
  }],
  "total": 58, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.sales.order_api.get_order`

**Get single order**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `order` | string | required |  |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.sales.order_api.create_order`

**Create order directly from inquiry** — No markup — the Quotation-sourced path is convert_to_order instead. `inquiry` is required (there's no third, source-less way to create one).

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |
| `lines` | array | required | [{ item, qty }] |
| `expected_dispatch` | date | required |  |
| `inquiry` | string | required |  |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.sales.order_api.advance_order_stage`

**Advance stage** — Validated as a strict forward step (or a Cancel from anywhere but Delivered). Entering Picking auto-creates Pick Tasks.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `order` | string | required |  |
| `next_stage` | string | required |  |
| `note` | string | optional |  |

**Response**

```json
(same shape as one list row)
```

> total is server-computed (native grand_total) — every line's rate came from get_dealer_price at creation, never a client-supplied value


---

## WhatsApp / Communications

System of record + webhook contract for a (not-yet-built) middleware layer that talks to the real WhatsApp Business API.

#### GET `dms_erp.comms.api.list_messages`

**Message log for a dealer** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "row-1", "dealerId": "CUST-0004", "direction": "Inbound", "text": "Stock available for PVT-6060?",
    "status": "Delivered", "relatedType": "Inquiry", "relatedRef": "INQ-2026-00042",
    "sentAt": "2026-08-29 09:40:00", "sentBy": null
  }],
  "total": 14, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.comms.api.last_message`

**Last message for a dealer**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |

**Response**

```json
(one message, or null)
```


#### GET `dms_erp.comms.api.unreplied_inbound_count`

**Unreplied-inbound count** — 0 or 1 — whether the most recent inbound message still has no outbound reply after it.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |

**Response**

```json
1
```


#### GET `dms_erp.comms.api.list_templates`

**List message templates**

**Params**

_No parameters._

**Response**

```json
[{ "id": "stock_available", "text": "Good news — {{item}} is back in stock." }]
```


#### POST `dms_erp.comms.api.send_message`

**Send message** — Sales/Management only. Free text or a filled template; links back to the triggering record.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | required |  |
| `text` | string | required |  |
| `related_type` | string | default "General" | e.g. Inquiry | Quotation | Order |
| `related_reference` | string | optional |  |

**Response**

```json
(same shape as one list_messages row, status "Sent")
```

> ⚠️ only inserts the log row — no real transport call yet, since the WhatsApp Business API integration is middleware-side per the BRD


#### POST `dms_erp.comms.api.mark_read`

**Mark read**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `message` | string | required |  |

**Response**

```json
(same shape, status "Read")
```


#### POST `dms_erp.comms.api.webhook_inbound_message` · `guest` (no Bearer token required)

**Webhook: inbound message** — Called by the middleware when a dealer sends a message. Secret-verified, not role-gated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `secret` | string | required |  |
| `dealer` | string | required |  |
| `text` | string | required |  |
| `related_type` | string | default "General" |  |
| `related_reference` | string | optional |  |
| `sent_at` | datetime | optional |  |

**Response**

```json
(same shape as one list_messages row, direction "Inbound")
```


#### POST `dms_erp.comms.api.webhook_status_update` · `guest` (no Bearer token required)

**Webhook: delivery status** — Real Sent → Delivered → Read / Failed progression from the transport layer.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `secret` | string | required |  |
| `message` | string | required |  |
| `status` | string | required |  |

**Response**

```json
(same shape, status updated)
```


---

## Warehouse — Bay Master

Bays are native ERPNext Warehouse records, nested under a physical warehouse (a "warehouse group"). No bespoke bay table. The physical-warehouse count isn't fixed — "Pacific Main — Morbi" and "Pacific Buffer — Wankaner" are just the initial seed (`warehouse/setup.py`'s `PHYSICAL_WAREHOUSES`); `create_warehouse_group` adds more at runtime.

#### GET `dms_erp.warehouse.bay_api.list_warehouse_groups`

**List / search bay groups (physical warehouses)** — for populating `create_bay`'s `parent_warehouse`. A group warehouse's real `name` isn't its clean display name — ERPNext autonames it with a company-abbreviation suffix (e.g. `"Pacific Main — Morbi - PI"`) — so this is the only place that raw id is exposed; `list_bays`/`get_bay_detail` resolve it back to the clean name on the way out, not in. No pagination — a physical-site directory stays small even as `create_warehouse_group` grows it past the original two, unlike the product/order lists.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `search` | string | optional | substring match on warehouse group name |

**Response**

```json
[
  { "id": "Pacific Buffer — Wankaner - PI", "name": "Pacific Buffer — Wankaner" },
  { "id": "Pacific Main — Morbi - PI", "name": "Pacific Main — Morbi" }
]
```


#### POST `dms_erp.warehouse.bay_api.create_warehouse_group`

**Create a bay group (physical warehouse)** — Warehouse/Management only. No `parent_warehouse` param — a group sits at the root, unlike a bay.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `name` | string | required | e.g. "Pacific Overflow — Rajkot" |

**Response**

```json
{ "id": "Pacific Overflow — Rajkot - PI", "name": "Pacific Overflow — Rajkot" }
```


#### GET `dms_erp.warehouse.bay_api.list_bays`

**List / search bays** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `search` | string | optional | substring match on bay code |
| `bay_type` | string | optional | exact match; main \| buffer \| damage \| insurance_claim \| display \| blocked |
| `status` | string | optional | exact match; active \| blocked \| reserved |
| `parent_warehouse` | string | optional | exact match on the raw, autonamed group `name` (as returned by `list_warehouse_groups`'s `id`), not the clean display name |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "Main Bay A-01 - PTC", "code": "A-01", "name": "Main Bay A-01",
    "warehouse": "Pacific Main — Morbi", "type": "main", "dimensions": "36x8",
    "capacityBoxes": 900, "suitableCategories": ["Vitrified"], "status": "active",
    "zone": "A", "row": "R1", "occupiedBoxes": 640, "occupancyPct": 71, "freeBoxes": 260
  }],
  "total": 48, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.warehouse.bay_api.get_bay_detail`

**Get single bay**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `code` | string | required | bay code, e.g. A-01 |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.warehouse.bay_api.create_bay`

**Create bay** — Warehouse/Management only.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `code` | string | required |  |
| `bay_type` | string | required | main | buffer | damage | insurance_claim | display | blocked |
| `dimensions` | string | required | 36x6 | 36x8 | 32x6 | 32x8 |
| `parent_warehouse` | string | required |  |
| `zone` | string | required |  |
| `row` | string | required |  |
| `suitable_categories` | string[] | optional |  |
| `capacity_boxes` | int | optional | defaults from a dimensions→capacity table if omitted |
| `status` | string | default "active" | active | blocked | reserved |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.warehouse.bay_api.create_bay_grid`

**Create bay grid (bulk)** — Warehouse/Management only. Skips any code that already exists.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `prefix` | string | required | e.g. "A" |
| `count` | int | required |  |
| `start_at` | int | required |  |
| `bay_type, dimensions, parent_warehouse, zone` | — | required | same as create_bay |
| `categories` | string[] | optional |  |

**Response**

```json
{ "created": 12 }
```


#### POST `dms_erp.warehouse.bay_api.update_bay`

**Update bay** — Warehouse/Management only. Patch-style.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `code` | string | required |  |
| `patch` | object | required | keys: name, type, dimensions, capacityBoxes, status, zone, row, warehouse, suitableCategories |

**Response**

```json
(same shape as one list row)
```


#### DELETE `dms_erp.warehouse.bay_api.delete_bay`

**Delete bay** — Warehouse/Management only. ERPNext itself refuses deletion if the bay has stock/ledger history.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `code` | string | required |  |

**Response**

```json
{ "success": true }
```


---

## Warehouse — Stock / Lots

A live aggregate over native Stock Ledger Entry, grouped by item+bay+batch — not a stored lot table.

#### GET `dms_erp.warehouse.stock_api.list_stock`

**List lots**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `bay` | string | optional |  |
| `item` | string | optional |  |

**Response**

```json
[{
  "id": "Main Bay A-01 - PTC::PVT-6060::BATCH-2608-11", "bayId": "Main Bay A-01 - PTC",
  "itemCode": "PVT-6060", "productId": "PVT-6060", "itemName": "Marbella Beige Vitrified 600x600",
  "category": "Vitrified", "batchNumber": "BATCH-2608-11", "boxes": 640,
  "storedAt": "2026-08-20", "damageType": null, "claimRef": null
}]
```

> damageType/claimRef are only ever non-null for a lot sitting in a damage or insurance-claim bay


#### GET `dms_erp.warehouse.stock_api.suggest_bays`

**Suggest bays for an allocation** — Scored by category match, free capacity, and existing-mix compatibility.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `category` | string | required |  |
| `qty` | number | required |  |
| `kind` | string | default "normal" | normal | damage | claim — routes to main/buffer vs. damage/insurance_claim bay types |

**Response**

```json
{
  "main": [{ "bay": { "id": "...", "code": "A-01", "...": "full bay shape" },
    "free": 260, "pct": 71, "score": 85, "reason": "Full quantity fits" }],
  "buffer": []
}
```


#### GET `dms_erp.warehouse.stock_api.validate_allocation`

**Validate allocation** — Capacity/compatibility checks before committing.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `bay` | string | required | bay code |
| `qty` | number | required |  |
| `category` | string | required |  |

**Response**

```json
[{ "level": "warning", "message": "A-01 already holds Wall Tiles. Mixing categories in one bay." }]
```


#### GET `dms_erp.warehouse.allocation_api.list_allocations`

**List allocation slips** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `inward_truck` | string | optional |  |
| `status` | string | optional |  |
| `item` | string | optional |  |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{ "items": [(get_allocation shape)], "total": 5, "limit": 20, "offset": 0 }
```


#### GET `dms_erp.warehouse.allocation_api.get_allocation`

**Get allocation slip**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `allocation` | string | required |  |

**Response**

```json
{
  "id": "BAS-2026-0007", "slipNumber": "BAS-2026-0007", "inwardTruck": "IWT-2026-0021",
  "purchaseOrder": "PUR-ORD-2026-00014", "itemCode": "PVT-6060", "batchNumber": "BATCH-2608-11",
  "totalQty": 640, "status": "Confirmed", "purchaseReceipt": "PREC-2026-00019",
  "allocations": [{ "bayId": "Main Bay A-01 - PTC", "bayCode": "A-01", "qty": 640, "confirmed": true }],
  "createdAt": "2026-08-20 14:02:11"
}
```


#### POST `dms_erp.warehouse.allocation_api.create_allocation`

**Create allocation** — Warehouse/Management only. Posts and submits a native Purchase Receipt (one row per bay split) — that's the real stock-effecting event, not a bespoke ledger.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `item` | string | required |  |
| `batch_no` | string | required |  |
| `total_qty` | number | required |  |
| `lines` | array | required | [{ bay: bay code, qty }], must sum to total_qty |
| `inward_truck` | string | optional |  |
| `supplier` | string | optional, required if no inward_truck |  |

**Response**

```json
(same shape as get_allocation)
```


#### POST `dms_erp.warehouse.allocation_api.mark_allocation_printed`

**Mark printed**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `allocation` | string | required |  |

**Response**

```json
(same shape as get_allocation, status "Printed")
```


#### GET `dms_erp.warehouse.allocation_api.get_allocation_qr_codes`

**Get QR codes for a slip** — One PNG per bay split, generated on demand (nothing stored). Each payload is the exact string Scan already parses.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `allocation` | string | required |  |

**Response**

```json
[{ "bayId": "Main Bay A-01 - PTC", "bayCode": "A-01", "qty": 640,
  "payload": "PI-ITEM|PVT-6060|BATCH-2608-11|A-01", "qrCode": "data:image/png;base64,iVBORw0KG..." }]
```


#### POST `dms_erp.warehouse.allocation_api.confirm_putaway`

**Confirm put-away** — Floor confirmation only, after a scan — does not move stock again.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `allocation` | string | required |  |

**Response**

```json
(same shape as get_allocation, status "Placed")
```


---

## Warehouse — Transfers

Native Stock Entry (Material Transfer) — not a custom doctype.

#### GET `dms_erp.warehouse.transfer_api.list_transfers`

**List transfers** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "MAT-STE-2026-00081", "ref": "MAT-STE-2026-00081", "itemCode": "PVT-6060",
    "batchNumber": "BATCH-2608-11", "fromBayId": "Main Bay A-01 - PTC", "toBayId": "Damage Bay D-01 - PTC",
    "qty": 12, "transferType": "Damage→Insurance Claim", "reason": "Insurance Claim",
    "damageType": "Broken", "claimRef": null, "remarks": null,
    "transferredAt": "2026-08-29", "transferredBy": "raj@pacific.example"
  }],
  "total": 37, "limit": 20, "offset": 0
}
```


#### POST `dms_erp.warehouse.transfer_api.transfer_stock`

**Transfer stock between bays** — Warehouse/Management only. Native Stock Entry — capacity and available-qty are checked before posting.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `from_bay, to_bay` | string | required | bay codes |
| `item` | string | required |  |
| `batch_no` | string | required |  |
| `qty` | number | required |  |
| `transfer_type` | string | required | Main→Buffer | Buffer→Main | Main→Damage | Damage→Insurance Claim | Display→Main | Warehouse→Dealer Display |
| `reason` | string | required | Reallocation | Damage Identified | Insurance Claim | Display Setup | Consolidation | Other |
| `remarks` | string | optional |  |
| `damage_type` | string | optional | required in practice for a damage-related transfer |
| `claim_ref` | string | optional |  |

**Response**

```json
(same shape as one list row)
```


---

## Warehouse — Scan

One read-only lookup backing inward placement, transfer, picking, and bay-audit scan flows alike.

#### GET `dms_erp.warehouse.allocation_api.resolve_scan`

**Resolve scanned code** — Understands PI-BAY|/PI-ITEM|-prefixed codes plus bare bay/item/batch manual entry.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `code` | string | required |  |

**Response**

```json
{ "ok": true, "kind": "item",
  "lot": { "itemCode": "PVT-6060", "batchNumber": "BATCH-2608-11", "boxes": 640, "bayId": "Main Bay A-01 - PTC" },
  "message": "PVT-6060 · BATCH-2608-11 — 640 boxes in Main Bay A-01 - PTC" }
```

> kind is "bay", "item", or "unknown" (with ok:false and a human message) if nothing matches


---

## Inward

Gate/LR/ETA tracking — the actual stock effect happens later, at allocation.

> ⚠️ "Mark allocated" isn't a standalone endpoint — it's a side effect of Stock/Lots' create_allocation (sets allocationSlip + batchNumber on the truck automatically)
> ✕ no shortage/claim capture or supplier invoice reference on receipt — confirmed not modeled anywhere in Inward Truck or the Purchase Receipt it posts

#### GET `dms_erp.warehouse.inward_api.list_trucks`

**List trucks** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "IWT-2026-0021", "lr": "LR-88213", "supplier": "Orient Ceramics", "vehicle": "GJ-05-XX-1234",
    "eta": "2026-08-20", "boxes": 640, "status": "Put-away", "item": "PVT-6060", "batchNumber": "BATCH-2608-11",
    "poReference": null, "poId": "PUR-ORD-2026-00014", "poLineId": "row-9", "allocationSlip": "BAS-2026-0007"
  }],
  "total": 22, "limit": 20, "offset": 0
}
```


#### POST `dms_erp.warehouse.inward_api.add_truck`

**Create inward truck** — Warehouse/Purchase/Management. PO link is optional.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `supplier` | string | required |  |
| `item` | string | required |  |
| `boxes` | int | required |  |
| `lr_number` | string | optional, auto-generated if omitted |  |
| `vehicle_number` | string | optional |  |
| `eta` | date | optional |  |
| `purchase_order, purchase_order_item` | string | optional |  |
| `po_reference` | string | optional | free-text, when there's no real PO link |

**Response**

```json
(same shape as one list row, status "Scheduled")
```


#### POST `dms_erp.warehouse.inward_api.advance_truck`

**Advance status** — Scheduled → At Gate → Unloading → Put-away.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `truck` | string | required |  |
| `next_status` | string | required |  |

**Response**

```json
(same shape as one list row)
```


---

## Picking

Custom Pick Task doctype — one row per order line, finer-grained than ERPNext's native Pick List.

> ⚠️ "Available lots for a product" maps to Stock/Lots' list_stock(item=...) — the general warehouse stock endpoint, not something picking-specific

#### GET `dms_erp.sales.picking_api.list_pick_tasks`

**List pick tasks** — Created automatically when an order enters the Picking stage. Paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `order` | string | optional |  |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "PT-2026-00061", "orderNumber": "SAL-ORD-2026-00033", "itemCode": "PVT-6060",
    "batchNumber": null, "qty": 60, "allocated": 0, "suggestedBayId": "Main Bay A-01 - PTC",
    "picker": null, "status": "Pending"
  }],
  "total": 9, "limit": 20, "offset": 0
}
```


#### POST `dms_erp.sales.picking_api.patch_task`

**Patch task** — Warehouse/Management only. Patch-style.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `task` | string | required |  |
| `patch` | object | required | keys: picker, status, allocated, batchNumber, suggestedBayId |

**Response**

```json
(same shape as one list row)
```

> ⚠️ no scan-code verification wired into this call — nothing stops marking Picked without a matching scan


#### POST `dms_erp.sales.picking_api.auto_allocate`

**Auto-allocate** — Fills allocated up to available stock; status becomes Allocated if any qty was assigned.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `task` | string | required |  |

**Response**

```json
(same shape as one list row)
```


---

## Purchase Orders

Native ERPNext Purchase Order, submitted immediately ("Raise PO" is one action).

#### GET `dms_erp.purchase.po_api.list_purchase_orders`

**List purchase orders** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "PUR-ORD-2026-00014", "number": "PUR-ORD-2026-00014", "date": "2026-08-10",
    "supplier": "Orient Ceramics", "expectedReadyDate": "2026-08-20", "remarks": null,
    "sourceInquiry": null,
    "lines": [{ "id": "row-9", "itemCode": "PVT-6060", "itemName": "Marbella Beige Vitrified 600x600",
      "orderedQty": 640, "readyQty": 640, "receivedQty": 640 }]
  }],
  "total": 18, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.purchase.po_api.get_purchase_order`

**Get single PO**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `po` | string | required |  |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.purchase.po_api.create_purchase_order`

**Create purchase order** — Purchase/Management only.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `item` | string | required |  |
| `ordered_qty` | number | required |  |
| `supplier` | string | required |  |
| `expected_ready_date` | date | required |  |
| `remarks` | string | optional |  |
| `source_inquiry` | string | optional | set when raised via Inquiries' convert_to_purchase_requirement |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.purchase.po_api.set_line_ready`

**Set line ready qty** — Supplier-confirmed readiness — clamped to [0, orderedQty].

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `po` | string | required |  |
| `line` | string | required |  |
| `ready_qty` | number | required |  |

**Response**

```json
(one line row)
```


#### GET `dms_erp.purchase.po_api.line_progress`

**Line progress**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `line` | string | required |  |

**Response**

```json
{ "plannedQty": 640, "receivedQty": 640, "remainingToPlan": 0, "status": "Fully planned" }
```

> ⚠️ status derives from plannedQty (booked inward trucks) vs. ready vs. received — ordered/ready qty themselves come from the PO's own line list, not repeated here


---

## Pickup Run

Capacity-aware planning layer in front of supplier-ready PO lines — groups them onto one truck per supplier, checked against a Vehicle Type's box capacity before booking.

#### GET `dms_erp.purchase.pickup_run_api.list_vehicle_types`

**List vehicle types** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{ "id": "VEH-TYPE-0001", "name": "Big Truck", "capacityBoxes": 900 }],
  "total": 4, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.purchase.pickup_run_api.list_pickup_runs`

**List pickup runs** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `supplier` | string | optional |  |
| `status` | string | optional | Draft \| Dispatched \| Completed \| Cancelled |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "PICKUP-RUN-2026-0004", "supplier": "Orient Ceramics", "vehicleType": "VEH-TYPE-0001",
    "vehicleNumber": "GJ-05-XX-9999", "scheduledDate": null, "status": "Draft", "totalBoxes": 200,
    "lines": [{ "purchaseOrder": "PUR-ORD-2026-00014", "purchaseOrderItem": "row-9", "item": "PVT-6060", "qty": 200 }]
  }],
  "total": 6, "limit": 20, "offset": 0
}
```


---

## Purchase Requirements / Reorder Planning

Fully real formula as of Phase 10+13: current stock, missed demand, pending inquiries, retail sales velocity, and open-PO coverage.

#### GET `dms_erp.purchase.reorder_api.reorder_suggestions`

**Reorder suggestions** — Retail/sub-dealer channel only, urgency-ranked (Critical → High → Watch → Healthy).

**Params**

_No parameters._

**Response**

```json
[{
  "productId": "PVT-6060", "currentStock": 40, "missedDemandQty": 15, "pendingInquiryQty": 30,
  "recentRetailSalesQty": 180, "openPurchaseOrderQty": 0, "openPurchaseOrders": [],
  "suggestedQty": 130, "urgency": "High",
  "reasons": ["15 boxes of missed/constrained retail demand", "30 boxes in open retail inquiries",
    "~21 boxes of expected demand across a 21-day lead time (180 boxes sold in the last 180 days)",
    "Below 100-box safety stock"],
  "nonReorderable": false
}]
```

> ⚠️ Factory Discontinued / Display Removal Pending / Pulled Back items still appear in this array (suggestedQty 0, nonReorderable true) rather than being filtered out entirely
> openPurchaseOrderQty already nets out of suggestedQty, so an item fully covered by an open PO naturally drops to 0 without a separate "covered" flag


#### POST `dms_erp.sales.inquiry_api.convert_to_purchase_requirement` · **PARTIAL**

**Raise PO from a suggestion** — There's no suggestion-id to post against (a suggestion is a live per-item computation, not a stored row) — this converts a specific driving Inquiry into a real, linked PO instead.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `inquiry` | string | required | must be Open, Out of Stock, or Pre-order Required |
| `supplier` | string | required |  |
| `expected_ready_date` | date | required |  |
| `ordered_qty` | number | optional, defaults to the inquiry's qty |  |
| `remarks` | string | optional |  |

**Response**

```json
(Purchase Order shape — see Purchase Orders above)
```

> ⚠️ flips the source Inquiry to "Mapped to PO". For a suggestion with no single driving Inquiry, call Purchase Orders' generic create_purchase_order directly with the item/suggestedQty instead.


---

## Damage & Insurance Claims

One claim per Damage→Insurance Claim Stock Entry. Settlement GL posting is fully config-gated — see the accounting box below.

> **DMS Accounting Settings (Single doctype)**
> post_accounting_entries (Check, default unchecked), default_company, default_bank_account, insurance_claim_receivable_account, insurance_settlement_variance_account (nullable — only needed if a settlement's amount differs from the claimed amount), unloading_expense_account. While the flag is off, update_claim_status is a pure status/amount write. When it's on, the accounts a settlement needs are verified first — a missing one raises ValidationError naming exactly what's missing, never a guessed account — then a Journal Entry posts (debit bank for what was received, credit the receivable account for the full claimed amount, route any delta through the variance account) and links back via the new settlementJournalEntry field.

#### GET `dms_erp.finance.claims_api.list_claims`

**List claims** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `status` | string | optional | Filed | Approved | Settled | Rejected |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "CLM-2026-0009", "claimRef": "CLM-2026-0009", "stockEntry": "MAT-STE-2026-00081",
    "itemCode": "PVT-6060", "batchNumber": "BATCH-2608-11", "qty": 12,
    "insurer": "HDFC Ergo", "claimAmount": 25600, "status": "Filed",
    "filedAt": "2026-08-29", "filedBy": "raj@pacific.example",
    "settledAmount": null, "settledAt": null, "settlementJournalEntry": null, "remarks": "Transit damage"
  }],
  "total": 11, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.finance.claims_api.get_claim`

**Get single claim**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `claim` | string | required |  |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.finance.claims_api.file_claim`

**File claim** — Warehouse/Management only. One claim per damage transfer — writes back to that transfer's claimRef.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `stock_entry` | string | required | must be a Damage→Insurance Claim transfer |
| `insurer` | string | required |  |
| `claim_amount` | number | required |  |
| `remarks` | string | optional |  |

**Response**

```json
(same shape as one list row)
```


#### POST `dms_erp.finance.claims_api.update_claim_status`

**Update status (incl. Settle)** — Filed → Approved → Settled / Rejected. Status/amount always update; GL posting is additive and config-gated (see box above).

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `claim` | string | required |  |
| `status` | string | required |  |
| `settled_amount` | number | optional, defaults to claimAmount when status="Settled" |  |

**Response**

```json
(same shape as one list row, with settlementJournalEntry set only if DMS Accounting Settings has posting on)
```


#### GET `dms_erp.finance.claims_api.claim_summary`

**Claim summary**

**Params**

_No parameters._

**Response**

```json
{ "receivable": 25600, "settled": 22400, "rejected": 1 }
```


---

## Unloading Payment

One voucher per Inward Truck. Same accounting-settings pattern as Claims.

> **Same DMS Accounting Settings doctype — not a separate settings object**
> mark_paid always updates status/paidBy/paidAt regardless of the flag. When posting is on and unloading_expense_account + default_bank_account are configured, it additionally posts a Payment Entry (Internal Transfer — there's no real ERPNext Party for a labour contractor): debit the expense account, credit the bank account, linked back via the new paymentEntry field.

#### GET `dms_erp.finance.unloading_api.list_charges`

**List charges** — paginated.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `status` | string | optional | Pending | Paid |
| `limit` | int | optional, default 20, max 100 | page size |
| `offset` | int | optional, default 0 | rows to skip |

**Response**

```json
{
  "items": [{
    "id": "UNL-2026-0018", "voucherNumber": "UNL-2026-0018", "truckId": "IWT-2026-0021", "lr": "LR-88213",
    "contractor": "Morbi Labour Contractors", "boxes": 640, "ratePerBox": 6, "chargeAmount": 3840,
    "paymentMode": "Cash", "status": "Pending", "recordedAt": "2026-08-20", "recordedBy": "raj@pacific.example",
    "paidBy": null, "paidAt": null, "paymentEntry": null, "remarks": null
  }],
  "total": 15, "limit": 20, "offset": 0
}
```


#### GET `dms_erp.finance.unloading_api.get_charge_for_truck`

**Get charge for a truck** — Returns null if none recorded yet.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `inward_truck` | string | required |  |

**Response**

```json
(same shape as one list row, or null)
```


#### POST `dms_erp.finance.unloading_api.record_charge`

**Record charge** — Warehouse/Management only. One per truck; boxes/amount derive from the linked truck, never re-entered.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `inward_truck` | string | required |  |
| `contractor` | string | required |  |
| `rate_per_box` | number | required |  |
| `payment_mode` | string | required | Cash | Bank Transfer | UPI | Cheque |
| `remarks` | string | optional |  |

**Response**

```json
(same shape as one list row, status "Pending")
```


#### POST `dms_erp.finance.unloading_api.mark_paid`

**Mark paid**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `charge` | string | required |  |

**Response**

```json
(same shape as one list row, status "Paid", paidBy/paidAt stamped from the caller/now)
```

> ⚠️ always stamps paidBy as the calling user and paidAt as today — no caller-supplied override for either


---

## Dashboard

Pure read/aggregation over every other module — no doctype, nothing stored, nothing static.

#### GET `dms_erp.dashboard.api.get_dashboard`

**Role-agnostic entry point** — resolves the caller's own primary role (the same precedence `auth.api.login`'s `user.primary_role` uses) and returns that role's **ERPNext-native** `Dashboard` doc's widgets, so the frontend never has to know which of `sales_dashboard`/`purchase_dashboard`/`warehouse_dashboard`/`management_dashboard` to call, and an admin can add/remove/reorder a KPI card or chart from the desk UI with no code change or deploy. A System Manager-only account (no `DMS *` role — the admin escape hatch documented in `auth/api.py`) resolves to `"management"`.

**Setup required**: a System Manager creates one `Dashboard` doc per role in the desk UI (Dashboard List → New), named exactly:

| Role | Dashboard doc name |
|---|---|
| sales | `Sales Dashboard` |
| purchase | `Purchase Dashboard` |
| warehouse | `Warehouse Dashboard` |
| management | `Management Dashboard` |

...then adds `Number Card`/`Dashboard Chart` widgets to it the normal ERPNext way. Until that Dashboard doc exists, `get_dashboard` returns an empty `widgets` list for that role rather than erroring — this endpoint ships ahead of that configuration being done.

**Params**

_No parameters._

**Response**

```json
{
  "role": "sales",
  "widgets": [
    { "type": "number_card", "name": "Today's Inquiries", "label": "Today's Inquiries", "value": 7, "color": "#29CD42", "trend": null },
    { "type": "chart", "name": "Inquiry Trend", "label": "Inquiry Trend", "chart_type": "line", "color": "#7C4DFF",
      "labels": ["24-07-2026", "25-07-2026"], "datasets": [{ "name": "Inquiry Trend", "values": [12, 18] }] }
  ]
}
```

Each widget's `type` is either `"number_card"` (with `value`, optional `trend` percentage) or `"chart"` (with `chart_type`: `line`/`bar`/`percentage`/`pie`/`donut`/`heatmap`, and either `labels`+`datasets`, or for heatmap charts `labels: []`+`dataPoints`). Widgets are permission-filtered individually by Frappe's own `Dashboard.get_permitted_cards`/`get_permitted_charts` — one scoped to a doctype/report the caller can't read is silently omitted, not surfaced as an error. The four `sales_dashboard`/`purchase_dashboard`/`warehouse_dashboard`/`management_dashboard` endpoints below are unaffected and still independently callable/whitelisted, but `get_dashboard` no longer routes through them.

> ⚠️ throws `PermissionError` if the caller holds none of the four DMS roles (and isn't System Manager)


#### GET `dms_erp.dashboard.api.sales_dashboard`

**Sales dashboard** — Sales/Management.

**Params**

_No parameters._

**Response**

```json
{
  "todaysInquiries": 4, "pendingQuotations": 9,
  "ordersThisMonth": { "count": 21, "value": 1842000 },
  "missedDemandValue": 96000,
  "inquiryTrend": [{ "day": "2026-08-29", "inquiries": 4 }],
  "actionableInquiries": [{ "id": "INQ-2026-00042", "dealerId": "CUST-0004", "productId": "PVT-6060", "qty": 60, "status": "Open" }]
}
```


#### GET `dms_erp.dashboard.api.warehouse_dashboard`

**Warehouse dashboard** — Warehouse/Management.

**Params**

_No parameters._

**Response**

```json
{
  "kpis": { "totalBays": 42, "occupancyRatePct": 68, "itemsInBuffer": 6, "pendingAllocationsToday": 3, "damageAwaitingClaim": 2 },
  "alerts": [{ "id": "full-...", "title": "A-01 is at 96% capacity", "detail": "...", "priority": "High" }],
  "incomingTrucksToday": [{ "id": "IWT-2026-0022", "status": "At Gate", "...": "full truck shape" }]
}
```


#### GET `dms_erp.dashboard.api.purchase_dashboard`

**Purchase dashboard** — Purchase/Management.

**Params**

_No parameters._

**Response**

```json
{
  "pendingPOs": 5, "supplierDelays": 1, "pickupPlansThisWeek": 3, "reorderSuggestionsCount": 7,
  "purchaseTrend": [{ "month": "2026-03", "value": 4200000 }],
  "materialsReadyForPickup": [{ "po": "PUR-ORD-2026-00014", "supplier": "Orient Ceramics", "itemCode": "PVT-6060", "readyQty": 120 }]
}
```


#### GET `dms_erp.dashboard.api.management_dashboard`

**Management dashboard** — Management only.

**Params**

_No parameters._

**Response**

```json
{
  "totalSalesMtd": 1842000, "outstandingReceivables": 0, "claimableValue": 25600,
  "topMovingItem": { "itemCode": "PVT-6060", "unitsSold": 640, "currentStock": 1840 },
  "salesByDealer": [{ "dealer": "CUST-0004", "value": 620000 }],
  "alerts": [{ "id": "credit-CUST-0004", "title": "Credit limit exceeded (by order value)", "detail": "...", "priority": "High" }]
}
```

> ⚠️ outstandingReceivables is a hard 0 — real AR needs a Sales Invoice + Payment Entry ledger this app's BRD scope (stops at "Dispatched") never includes. The credit-exposure alert uses order value, not true net receivables, for the same reason.


---

## Reports — Sales

BRD "Reports and Dashboards" — the report half. Filterable, dealer/date-range/status-scoped views over Inquiry/Quotation/Sales Order data, distinct from Dashboard's fixed per-role KPI snapshot. No role gate on the read itself, same as every other list/get endpoint in this app.

#### GET `dms_erp.reports.sales_reports.dealer_inquiry_report`

**Dealer inquiry report** — Every inquiry for a dealer (or across all dealers), with a status breakdown.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | optional |  |
| `status` | string | optional | one of Inquiry's 10 lifecycle states |
| `from_date, to_date` | date | optional | filters on Inquiry.date |

**Response**

```json
{
  "rows": [{ "id": "INQ-2026-00042", "dealerId": "CUST-0004", "productId": "PVT-6060", "qty": 60, "status": "Open", "...": "same shape as one row of list_inquiries' items" }],
  "summary": { "total": 14, "byStatus": { "Open": 6, "Quoted": 3, "Closed": 5 } }
}
```


#### GET `dms_erp.reports.sales_reports.missed_demand_report`

**Missed demand report** — Every Out of Stock / Pre-order Required inquiry, priced at the approved dealer price — the row-level version of the sales dashboard's single missedDemandValue.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `from_date, to_date` | date | optional | filters on Inquiry.date |

**Response**

```json
{
  "rows": [{ "id": "INQ-2026-00051", "dealerId": "CUST-0004", "productId": "PVT-6060", "qty": 15, "status": "Out of Stock", "estimatedValue": 8400 }],
  "totalValue": 96000
}
```

> rows sorted by estimatedValue descending


#### GET `dms_erp.reports.sales_reports.retail_vs_bulk_report`

**Retail vs bulk report** — Order count and value grouped by Sales Order.custom_order_channel (Retail | Bulk | Project) — unblocked entirely by that Phase 15 field.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `from_date, to_date` | date | optional | filters on Sales Order.transaction_date |

**Response**

```json
{
  "byChannel": [
    { "channel": "Retail", "orderCount": 18, "value": 1420000 },
    { "channel": "Bulk", "orderCount": 2, "value": 890000 },
    { "channel": "Project", "orderCount": 0, "value": 0 }
  ]
}
```


#### GET `dms_erp.reports.sales_reports.dealer_activity_report`

**Dealer activity report** — Per-dealer rollup across Inquiry, Quotation, Sales Order, and WhatsApp Message — no single existing list/get function crosses these on its own.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `dealer` | string | optional | single dealer only; omit for every dealer |

**Response**

```json
[{
  "dealerId": "CUST-0004", "dealerName": "Shree Ganesh Tiles",
  "inquiryCount": 9, "quotationCount": 5, "orderCount": 4, "orderValue": 620000,
  "messageCount": 12, "lastContact": "2026-08-29 09:40:00"
}]
```

> sorted by orderValue descending


#### GET `dms_erp.reports.sales_reports.duplicate_inquiry_report`

**Duplicate inquiry report** — Two or more still-open inquiries (not yet Converted to Order / Mapped to PO / Rejected / Closed), same dealer and item, logged within window_days of each other. No BRD-specified duplicate rule existed — this is a proposed one, easy to retune.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `window_days` | int | optional, default 7 |  |

**Response**

```json
[{
  "dealerId": "CUST-0004", "productId": "PVT-6060", "count": 2,
  "inquiries": [{ "id": "INQ-2026-00042", "date": "2026-08-29", "qty": 60, "status": "Open" },
    { "id": "INQ-2026-00044", "date": "2026-08-30", "qty": 40, "status": "Open" }]
}]
```


---

## Reports — Warehouse

Filterable views over bay/stock data, plus two proposed heuristics (Stock Clearance, Display Replacement) with no BRD-specified formula — both built from small, named, retunable constants rather than magic numbers.

#### GET `dms_erp.reports.warehouse_reports.bay_occupancy_report`

**Bay occupancy report**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `warehouse` | string | optional |  |
| `bay_type` | string | optional | main | buffer | damage | insurance_claim | display | blocked |

**Response**

```json
{
  "rows": [{ "id": "Main Bay A-01 - PTC", "code": "A-01", "occupiedBoxes": 640, "capacityBoxes": 900, "occupancyPct": 71, "...": "full bay shape" }],
  "summary": { "bayCount": 42, "avgOccupancyPct": 68, "fullBays": 3 }
}
```


#### GET `dms_erp.reports.warehouse_reports.visual_stock_balance`

**Visual stock balance** — Every bay with the lots physically in it — shaped for a bay-grid/heatmap UI.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `warehouse` | string | optional |  |

**Response**

```json
[{ "id": "Main Bay A-01 - PTC", "code": "A-01", "...": "full bay shape",
  "lots": [{ "itemCode": "PVT-6060", "batchNumber": "BATCH-2608-11", "boxes": 640, "storedAt": "2026-08-20" }]
}]
```


#### GET `dms_erp.reports.warehouse_reports.stock_clearance_suggestions`

**Stock clearance suggestion** — Flags an item when current stock is more than 2x the safety-stock floor, trailing-window Retail sales velocity hasn't covered even one floor's worth, and its oldest batch is 90+ days old — all three at once.

**Params**

_No parameters._

**Response**

```json
[{
  "productId": "PVT-9090", "itemName": "Onyx Grey Vitrified 900x900", "currentStock": 260,
  "recentSalesQty": 20, "oldestBatchAgeDays": 134,
  "reason": "260 boxes on hand vs. a 100-box safety floor, only 20 sold in the last 180 days, oldest batch 134 days old"
}]
```

> sorted by oldestBatchAgeDays descending; CLEARANCE_STOCK_MULTIPLE / CLEARANCE_MIN_AGE_DAYS are named constants in warehouse_reports.py — retune there if the thresholds are wrong


#### GET `dms_erp.reports.warehouse_reports.display_replacement_suggestions`

**Display replacement suggestion** — For every item on a display-type bay that's either not selling or past Active in the discontinuation lifecycle, suggests the fastest-moving currently-sellable item in the same category not already displayed.

**Params**

_No parameters._

**Response**

```json
[{
  "currentDisplayItem": "PVT-4545", "itemName": "Coastal Sand 450x450", "bayId": "Display Bay DS-01 - PTC",
  "reason": "no Retail sales in the last 180 days",
  "suggestedReplacement": "PVT-6060", "replacementReason": "fastest-moving Vitrified not currently displayed (180 boxes sold recently)"
}]
```

> ⚠️ suggestedReplacement can be null — no faster-moving, currently-sellable candidate found in that category


---

## Reports — Purchase

Filterable views over PO/reorder data — the same underlying computations as the purchase dashboard and reorder engine, exposed as report-shaped endpoints.

#### GET `dms_erp.reports.purchase_reports.reorder_planning_report`

**Purchase reorder planning report** — The reorder engine's own urgency-ranked suggestions, filtered for a planning meeting rather than a live dashboard tile.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `urgency` | string | optional | Critical | High | Watch | Healthy |
| `actionable_only` | bool | optional, default false | true to keep only rows with suggestedQty > 0 |

**Response**

```json
(same array shape as reorder_suggestions under Purchase Requirements / Reorder Planning above)
```


#### GET `dms_erp.reports.purchase_reports.purchase_pickup_plan`

**Purchase pickup plan** — Supplier-confirmed-ready material not yet booked onto a truck — same data as the purchase dashboard's materialsReadyForPickup, as its own filterable report.

**Params**

_No parameters._

**Response**

```json
{
  "rows": [{ "po": "PUR-ORD-2026-00014", "supplier": "Orient Ceramics", "itemCode": "PVT-6060", "readyQty": 120 }],
  "summary": { "lineCount": 6, "totalReadyQty": 480 }
}
```


#### GET `dms_erp.reports.purchase_reports.inquiry_to_po_mapping_report`

**Inquiry-to-PO mapping report** — Every PO raised via convert_to_purchase_requirement, joined back to the Inquiry that drove it.

**Params**

_No parameters._

**Response**

```json
[{
  "inquiryId": "INQ-2026-00051", "dealerId": "CUST-0004", "productId": "PVT-6060",
  "inquiryQty": 15, "inquiryStatus": "Mapped to PO",
  "poId": "PUR-ORD-2026-00019", "poSupplier": "Orient Ceramics", "poOrderedQty": 15, "poReceivedQty": 0
}]
```

> a directly-raised PO (no source_inquiry) doesn't appear — an empty array is a valid result, not an error


#### GET `dms_erp.reports.purchase_reports.po_pending_report`

**PO pending report** — Wraps list_pending_po_lines with the supplier/overdue filters a purchase planning report wants.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `supplier` | string | optional |  |
| `overdue_only` | bool | optional, default false |  |

**Response**

```json
{
  "rows": [{ "po": "PUR-ORD-2026-00014", "supplier": "Orient Ceramics", "itemCode": "PVT-6060", "pendingQty": 40, "daysOverdue": 3 }],
  "summary": { "lineCount": 11, "totalPendingQty": 260, "overdueCount": 2 }
}
```


---

## Reports — Finance

Filterable views over claims and unloading-payment data.

#### GET `dms_erp.reports.finance_reports.damage_and_insurance_report`

**Damage and insurance report**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `status` | string | optional | Filed | Approved | Settled | Rejected |
| `insurer` | string | optional |  |
| `from_date, to_date` | date | optional | filters on the claim's filedAt |

**Response**

```json
{
  "rows": [{ "id": "CLM-2026-0009", "insurer": "HDFC Ergo", "claimAmount": 25600, "status": "Filed", "...": "full claim shape" }],
  "summary": { "count": 7, "totalClaimed": 142000, "totalSettled": 96400 }
}
```


#### GET `dms_erp.reports.finance_reports.claimable_value_report`

**Claimable value report** — claim_summary's totals, broken down by insurer.

**Params**

_No parameters._

**Response**

```json
{
  "receivable": 25600, "settled": 22400, "rejected": 1,
  "byInsurer": [{ "insurer": "HDFC Ergo", "receivable": 25600, "settled": 22400 }]
}
```


#### GET `dms_erp.reports.finance_reports.unloading_payment_report`

**Unloading payment report**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `status` | string | optional | Pending | Paid |
| `contractor` | string | optional |  |
| `from_date, to_date` | date | optional | filters on the charge's recordedAt |

**Response**

```json
{
  "rows": [{ "id": "UNL-2026-0018", "contractor": "Morbi Labour Contractors", "chargeAmount": 3840, "status": "Pending", "...": "full charge shape" }],
  "summary": { "count": 9, "totalPending": 12400, "totalPaid": 34200 }
}
```


---

## Reports — Catalog

Filterable views over pricing and per-item activity, plus a whole-catalog velocity ranking.

#### GET `dms_erp.reports.catalog_reports.pricing_and_csp_report`

**Pricing and CSP report** — CSP = Customer Suggested Price — the approved price record's own suggestedPrice (landing cost × (1 + margin %)), not a second lookup against the live dealer price. Only Approved price records are included.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `min_margin_pct` | number | optional | keep only rows with actualMarginPct >= this |

**Response**

```json
[{ "productId": "PVT-6060", "landingCost": 415, "targetMarginPct": 25, "csp": 519, "actualMarginPct": 20.04 }]
```

> sorted by actualMarginPct ascending — worst margins first


#### GET `dms_erp.reports.catalog_reports.product_movement_report`

**Fast/slow-moving product report** — Ranks the whole catalog by the same trailing-window Retail sales velocity the reorder engine computes per-item.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `order` | string | optional, default "slow" | "slow" or "fast" |

**Response**

```json
[{ "productId": "PVT-9090", "itemName": "Onyx Grey Vitrified 900x900", "category": "Vitrified", "recentSalesQty": 20 }]
```


#### GET `dms_erp.reports.catalog_reports.product_activity_report`

**Product activity report** — Per-item rollup across Inquiry, Sales Order, Stock Entry transfers, and price-approval history — same cross-module-rollup shape as Dealer Activity, but per item.

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `item` | string | optional | single item only; omit for the whole catalog |

**Response**

```json
[{ "productId": "PVT-6060", "itemName": "Marbella Beige Vitrified 600x600", "category": "Vitrified",
  "inquiryCount": 9, "orderQty": 640, "transferCount": 3, "priceChanges": 2 }]
```

> sorted by orderQty descending


---

## Reports — Forecasting

The BRD's "Forecasting dashboard". No forecasting methodology was specified in the BRD text — deliberately kept to the simplest defensible method: a trailing 12-week average of Retail sales velocity, projected flat across the requested horizon. Does not model seasonality or trend.

#### GET `dms_erp.reports.forecasting.demand_forecast`

**Demand forecast**

**Params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `weeks_ahead` | int | optional, default 4 | must be positive |

**Response**

```json
[{
  "productId": "PVT-6060", "itemName": "Marbella Beige Vitrified 600x600",
  "avgWeeklyQty": 10.0, "projectedQty": 40, "weeksAhead": 4,
  "method": "trailing-12-week average", "confidence": "low — no seasonality or trend modeled"
}]
```

> ⚠️ confidence is always "low" right now by design — surface it in the UI wherever a projected number is shown, don't present this as a precise forecast

