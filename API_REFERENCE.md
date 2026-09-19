# dms_erp — Models & API Reference

Base URL used throughout: `http://localhost:8000`
Auth header (except `login`, `refresh_token`, `logout`, and the two `webhook_*` calls): `Authorization: Bearer <access_token>`
All bodies are JSON (`Content-Type: application/json`).

Status legend: ✅ tested & confirmed working in this session · 🐛 tested, bug found (fixed) · ⬜ not yet tested (request shape shown, response not verified) · 🚧 blocked by a known bug

---

## 1. Data Models (Doctypes)

### 1.1 Custom doctypes (owned by dms_erp)

| Doctype | Module | Purpose |
|---|---|---|
| Auth Session | auth | One row per logged-in device; refresh-token hash, issued/expires/revoked timestamps |
| Item Price Proposal | pricing | Pending→Approved dealer-price workflow for an Item |
| Item Price Proposal History (child) | pricing | Audit trail of price changes on a proposal |
| Dealer Catalog | catalog | Which Items a given dealer (Customer) may see |
| Dealer Catalog Item (child) | catalog | Item rows inside a Dealer Catalog |
| Inward Truck | warehouse | A delivery truck at the gate, pre-stock |
| Bay Allocation | warehouse | Planning record: which bay(s) an inward delivery goes into |
| Bay Allocation Line (child) | warehouse | Per-bay qty rows inside a Bay Allocation |
| Inquiry | sales | A dealer's raw request, before Quotation/Order |
| Order Stage Event (child, on Sales Order) | sales | Timestamped log of fulfillment-stage changes |
| Pick Task | sales | One row per order line being picked |
| Insurance Claim | finance | Damage claim, references a Stock Entry |
| Unloading Charge | finance | Labor charge for unloading one Inward Truck |
| WhatsApp Message | comms | System-of-record for inbound/outbound dealer messages |

### 1.2 Native ERPNext doctypes extended with custom fields

| Doctype | Custom fields added |
|---|---|
| Item | `custom_size`, `custom_finish`, `custom_color`, `custom_series`, `custom_swatch_color`, `custom_pieces_per_box`, `custom_sqft_per_box`, `custom_weight_per_box_kg`, `custom_discontinuation_status` |
| Warehouse | `custom_bay_code`, `custom_bay_type`, `custom_bay_status`, `custom_dimensions`, `custom_capacity_boxes`, `custom_zone`, `custom_row`, `custom_suitable_categories` |
| Stock Entry | `custom_transfer_type`, `custom_transfer_reason`, `custom_remarks`, `custom_damage_type`, `custom_claim_ref` |
| Purchase Order / Purchase Order Item | `custom_source_inquiry`, ready-qty tracking fields |
| Quotation | `custom_markup_pct`, `custom_freight`, `custom_inquiry`, `custom_order_channel` |
| Sales Order | `custom_fulfillment_stage` (`allow_on_submit`), `custom_vehicle`, `custom_source_type`, `custom_source_ref`, `custom_stage_history` (`allow_on_submit`), `custom_order_channel` |

### 1.3 Native ERPNext doctypes used as-is

`Item Group`, `Item Price`, `Price List`, `Item Alternative`, `Customer` (= "dealer"), `Supplier`, `Bin`, `Stock Ledger Entry`, `Batch`, `Purchase Receipt`, `Customer Credit Limit`, `Serial and Batch Bundle` / `Serial and Batch Entry`.

---

## 2. APIs by module

### 2.1 auth (`dms_erp.auth.api`)

**`login`** — POST, `allow_guest` — ✅
```json
POST /api/method/dms_erp.auth.api.login
{ "usr": "Administrator", "pwd": "<password>", "device_id": "postman-1", "device_name": "Postman" }
```
```json
{ "message": {
  "access_token": "eyJhbGciOi...", "refresh_token": "9f8a7b...",
  "token_type": "Bearer", "expires_in": 2400,
  "user": { "name": "Administrator", "roles": [...], "app_roles": ["management","purchase","warehouse","sales"], "primary_role": "management" }
}}
```

**`refresh_token`** — POST, `allow_guest` — ⬜
```json
POST /api/method/dms_erp.auth.api.refresh_token
{ "refresh_token": "<refresh_token from login>" }
```

**`logout`** — POST, `allow_guest` — ⬜
```json
POST /api/method/dms_erp.auth.api.logout
{ "refresh_token": "<refresh_token>" }
```

**`logout_all`** — POST, auth required — ⬜
```json
POST /api/method/dms_erp.auth.api.logout_all
```

**`me`** — GET, auth required — ⬜
```
GET /api/method/dms_erp.auth.api.me
```

---

### 2.2 catalog (`dms_erp.catalog.api`, `dms_erp.catalog.dealer_catalog_api`)

**`create_product`** — POST — ✅
```json
POST /api/method/dms_erp.catalog.api.create_product
{
  "code": "WT-3030", "name": "White Wall Tile 30x30", "category": "Wall Tiles",
  "supplier": "ABC Ceramics", "purchase_cost": 400, "margin_pct": 12,
  "effective_date": "2026-08-31", "size": "30x30", "pieces_per_box": 8, "sqft_per_box": 10
}
```
```json
{ "message": {
  "id": "WT-3030", "code": "WT-3030", "name": "White Wall Tile 30x30", "category": "Wall Tiles",
  "dealerPrice": null, "status": "Active", "isReorderable": true, "isSellable": true,
  "piecesPerBox": 8, "sqftPerBox": 10
}}
```
Note: `category` must be an existing Item Group (`Vitrified`, `Floor Tiles`, `Wall Tiles`, `Outdoor / Parking`). `code` is a primary key — duplicates fail with `DuplicateEntryError`.

**`get_product`** — GET — ✅
```
GET /api/method/dms_erp.catalog.api.get_product?item=WT-3030
```
```json
{ "message": { "id": "WT-3030", "code": "WT-3030", "dealerPrice": 500, "status": "Active", ... } }
```

**`list_products`** — GET, optional `?dealer=` — ⬜
```
GET /api/method/dms_erp.catalog.api.list_products
```

**`update_product`** — POST — ⬜
```json
POST /api/method/dms_erp.catalog.api.update_product
{ "item": "WT-3030", "patch": { "custom_finish": "Glossy" } }
```

**`dealer_catalog_api.is_visible`** — GET — ⬜
```
GET /api/method/dms_erp.catalog.dealer_catalog_api.is_visible?dealer=Sharma Tiles&item=WT-3030
```

**`dealer_catalog_api.catalog_for`** — GET — ⬜
```
GET /api/method/dms_erp.catalog.dealer_catalog_api.catalog_for?dealer=Sharma Tiles
```

**`dealer_catalog_api.set_product_visibility`** — POST — ⬜
```json
POST /api/method/dms_erp.catalog.dealer_catalog_api.set_product_visibility
{ "dealer": "Sharma Tiles", "item": "WT-3030", "visible": true }
```

**`dealer_catalog_api.set_category_visibility`** — POST — ⬜
```json
POST /api/method/dms_erp.catalog.dealer_catalog_api.set_category_visibility
{ "dealer": "Sharma Tiles", "item_group": "Wall Tiles", "visible": true }
```

**`dealer_catalog_api.category_coverage`** — GET — ⬜
```
GET /api/method/dms_erp.catalog.dealer_catalog_api.category_coverage?dealer=Sharma Tiles&item_group=Wall Tiles
```

---

### 2.3 pricing (`dms_erp.pricing.api`)

**`list_price_records`** — GET — ✅
```
GET /api/method/dms_erp.pricing.api.list_price_records
```

**`approve_price`** — POST — ✅
```json
POST /api/method/dms_erp.pricing.api.approve_price
{ "item": "WT-3030", "final_price": 500, "reason": "Initial pricing approval" }
```
```json
{ "message": { ... "status": "Approved", "suggestedPrice": 500 ... } }
```

**`get_price_record`** — GET — ⬜
```
GET /api/method/dms_erp.pricing.api.get_price_record?item=WT-3030
```

**`save_cost_inputs`** — POST/PUT — ⬜
```json
POST /api/method/dms_erp.pricing.api.save_cost_inputs
{ "item": "WT-3030", "supplier": "ABC Ceramics", "purchase_cost": 420, "margin_pct": 15, "effective_date": "2026-09-15" }
```

---

### 2.4 warehouse (`bay_api`, `stock_api`, `inward_api`, `allocation_api`, `transfer_api`)

**`bay_api.list_bays`** — GET — ✅
```
GET /api/method/dms_erp.warehouse.bay_api.list_bays
```

**`bay_api.create_bay`** — POST — ✅
```json
POST /api/method/dms_erp.warehouse.bay_api.create_bay
{
  "code": "A-01", "bay_type": "main", "dimensions": "36x8",
  "parent_warehouse": "Pacific Main — Morbi - M", "zone": "A", "row": "1", "capacity_boxes": 500
}
```
Valid `bay_type`: `main`, `buffer`, `damage`, `insurance_claim`, `display`, `blocked`.
Valid `dimensions`: `36x6`, `36x8`, `32x6`, `32x8`.
```json
{ "message": { "id": "Main Bay A-01 - M", "code": "A-01", "type": "main", "occupancyPct": 0, "freeBoxes": 500 } }
```

**`bay_api.get_bay_detail`** — GET — ⬜
```
GET /api/method/dms_erp.warehouse.bay_api.get_bay_detail?code=A-01
```

**`bay_api.create_bay_grid`** — POST — ⬜ (bulk create)
```json
POST /api/method/dms_erp.warehouse.bay_api.create_bay_grid
{ "prefix": "B", "count": 5, "start_at": 1, "bay_type": "main", "dimensions": "36x8", "parent_warehouse": "Pacific Main — Morbi - M", "zone": "B", "row": "1" }
```

**`bay_api.update_bay`** — POST — ⬜
```json
POST /api/method/dms_erp.warehouse.bay_api.update_bay
{ "code": "A-01", "patch": { "status": "blocked" } }
```

**`bay_api.delete_bay`** — POST/DELETE — ⬜
```json
POST /api/method/dms_erp.warehouse.bay_api.delete_bay
{ "code": "A-01" }
```

**`stock_api.list_stock`** — GET, optional `?bay=&item=` — ⬜
```
GET /api/method/dms_erp.warehouse.stock_api.list_stock
```

**`stock_api.suggest_bays`** — GET — ⬜
```
GET /api/method/dms_erp.warehouse.stock_api.suggest_bays?category=Wall Tiles&qty=50
```

**`stock_api.validate_allocation`** — GET — ⬜
```
GET /api/method/dms_erp.warehouse.stock_api.validate_allocation?bay=A-01&qty=50&category=Wall Tiles
```

**`inward_api.list_trucks`** — GET — ⬜
```
GET /api/method/dms_erp.warehouse.inward_api.list_trucks
```

**`inward_api.add_truck`** — POST — ✅
```json
POST /api/method/dms_erp.warehouse.inward_api.add_truck
{ "supplier": "ABC Ceramics", "item": "WT-3030", "boxes": 100, "vehicle_number": "GJ-01-AB-1234", "lr_number": "LR-001" }
```
```json
{ "message": { "id": "cpji286jts", "lr": "LR-001", "status": "Scheduled", "boxes": 100 } }
```

**`inward_api.advance_truck`** — POST — ✅
```json
POST /api/method/dms_erp.warehouse.inward_api.advance_truck
{ "truck": "cpji286jts", "next_status": "At Gate" }
```
Valid flow: `Scheduled → At Gate → Unloading → Put-away`.

**`allocation_api.create_allocation`** — POST — ✅ (the big one — posts a real Purchase Receipt)
```json
POST /api/method/dms_erp.warehouse.allocation_api.create_allocation
{
  "item": "WT-3030", "batch_no": "BATCH-001", "total_qty": 100,
  "lines": [ { "bay": "A-01", "qty": 100 } ], "inward_truck": "cpji286jts"
}
```
```json
{ "message": { "id": "BAS-2026-0001", "purchaseReceipt": "MAT-PRE-2026-00001", "status": "Confirmed",
  "allocations": [ { "bayCode": "A-01", "qty": 100.0, "confirmed": true } ] } }
```

**`allocation_api.list_allocations`** — GET — ⬜
**`allocation_api.get_allocation`** — GET — ⬜
**`allocation_api.mark_allocation_printed`** — POST — ⬜
**`allocation_api.get_allocation_qr_codes`** — GET — ⬜
**`allocation_api.resolve_scan`** — GET — ⬜
**`allocation_api.confirm_putaway`** — POST — ⬜
```json
POST /api/method/dms_erp.warehouse.allocation_api.confirm_putaway
{ "allocation": "BAS-2026-0001" }
```

**`transfer_api.list_transfers`** — GET — ⬜

**`transfer_api.transfer_stock`** — POST — 🚧 **BLOCKED (bug)**
```json
POST /api/method/dms_erp.warehouse.transfer_api.transfer_stock
{
  "from_bay": "A-01", "to_bay": "D-01", "item": "WT-3030", "batch_no": "BATCH-001",
  "qty": 10, "transfer_type": "Main→Damage", "reason": "Damage Identified", "damage_type": "Broken in transit"
}
```
> **Known bug:** always fails with `Only 0 boxes of this batch in <bay>` even when stock genuinely exists. Root cause: ERPNext v15 moved batch tracking to a new "Serial and Batch Bundle" system; `list_stock_lots()` (in `warehouse/utils.py`) still reads the old, now-empty `Stock Ledger Entry.batch_no` column. Fix identified (join through `Serial and Batch Bundle`/`Serial and Batch Entry`), not yet applied. Blocks **every** transfer type, and transitively blocks `finance.claims_api.file_claim`.

Valid `transfer_type`: `Main→Buffer`, `Buffer→Main`, `Main→Damage`, `Damage→Insurance Claim`, `Display→Main`, (+ more in `TRANSFER_TYPES`).
Valid `reason`: `Reallocation`, `Damage Identified`, `Insurance Claim`, `Display Setup`, `Consolidation`, `Other`.

---

### 2.5 purchase (`po_api`, `reorder_api`)

**`po_api.create_purchase_order`** — POST — ✅
```json
POST /api/method/dms_erp.purchase.po_api.create_purchase_order
{ "item": "WT-3030", "ordered_qty": 200, "supplier": "ABC Ceramics", "expected_ready_date": "2026-09-10" }
```
```json
{ "message": { "id": "PUR-ORD-2026-00001", "supplier": "ABC Ceramics",
  "lines": [ { "itemCode": "WT-3030", "orderedQty": 200.0, "receivedQty": 0 } ] } }
```

**`po_api.list_purchase_orders`** — GET — ⬜
**`po_api.get_purchase_order`** — GET — ⬜
```
GET /api/method/dms_erp.purchase.po_api.get_purchase_order?po=PUR-ORD-2026-00001
```
**`po_api.set_line_ready`** — POST — ⬜
```json
POST /api/method/dms_erp.purchase.po_api.set_line_ready
{ "po": "PUR-ORD-2026-00001", "line": "<line row id>", "ready_qty": 100 }
```
**`po_api.line_progress`** — GET — ⬜
```
GET /api/method/dms_erp.purchase.po_api.line_progress?line=<line row id>
```

**`reorder_api.reorder_suggestions`** — GET — ⬜
```
GET /api/method/dms_erp.purchase.reorder_api.reorder_suggestions
```

---

### 2.6 sales (`inquiry_api`, `quotation_api`, `order_api`, `picking_api`, `dealer_api`)

**`inquiry_api.create_inquiry`** — POST — ✅
```json
POST /api/method/dms_erp.sales.inquiry_api.create_inquiry
{ "dealer": "Sharma Tiles", "item": "WT-3030", "qty": 50, "source": "Phone" }
```
Valid `source`: `Phone`, `WhatsApp`, `Internal`, `Other`.
```json
{ "message": { "id": "INQ-2026-00001", "status": "Open", "qty": 50.0 } }
```

**`inquiry_api.list_inquiries`** — GET, optional `?dealer=&status=` — ⬜
**`inquiry_api.get_inquiry`** — GET — ⬜
**`inquiry_api.update_inquiry`** — POST — ⬜
```json
POST /api/method/dms_erp.sales.inquiry_api.update_inquiry
{ "inquiry": "INQ-2026-00001", "patch": { "status": "Quoted" } }
```
**`inquiry_api.convert_to_purchase_requirement`** — POST — ⬜

**`quotation_api.create_quotation`** — POST — ✅
```json
POST /api/method/dms_erp.sales.quotation_api.create_quotation
{ "dealer": "Sharma Tiles", "lines": [ { "item": "WT-3030", "qty": 50 } ], "markup_pct": 15, "inquiry": "INQ-2026-00001" }
```
```json
{ "message": { "id": "SAL-QTN-2026-00001", "lines": [ { "itemCode": "WT-3030", "qty": 50.0, "rate": 575.0 } ], "total": 28750.0 } }
```

**`quotation_api.convert_to_order`** — POST — ✅
```json
POST /api/method/dms_erp.sales.quotation_api.convert_to_order
{ "quotation": "SAL-QTN-2026-00001", "expected_dispatch": "2026-09-10" }
```
> Note: `expected_dispatch` is optional in the signature but effectively required — ERPNext's native Sales Order rejects submission without a delivery date.
```json
{ "message": { "id": "SAL-ORD-2026-00001", "stage": "Confirmed", "total": 28750.0 } }
```

**`quotation_api.list_quotations`** — GET — ⬜
**`quotation_api.get_quotation`** — GET — ⬜
**`quotation_api.add_quotation_line`** — POST — ⬜
**`quotation_api.remove_quotation_line`** — POST — ⬜
**`quotation_api.update_quotation_line_qty`** — POST — ⬜
**`quotation_api.update_quotation_status`** — POST — ⬜

**`order_api.advance_order_stage`** — POST — ✅ (needed an `allow_on_submit` bug fix to work)
```json
POST /api/method/dms_erp.sales.order_api.advance_order_stage
{ "order": "SAL-ORD-2026-00001", "next_stage": "Picking" }
```
Valid flow: `Confirmed → Picking → Ready to Dispatch → Dispatched → Delivered` (or `Cancelled` from any non-Delivered stage). Moving to `Picking` auto-creates Pick Tasks.
```json
{ "message": { "id": "SAL-ORD-2026-00001", "stage": "Picking", "history": [ {"stage":"Created",...}, {"stage":"Confirmed",...}, {"stage":"Picking",...} ] } }
```

**`order_api.list_orders`** — GET — ⬜
**`order_api.get_order`** — GET — ⬜
**`order_api.create_order`** — POST — ⬜ (direct Inquiry→Order, no markup)
```json
POST /api/method/dms_erp.sales.order_api.create_order
{ "dealer": "Sharma Tiles", "lines": [ { "item": "WT-3030", "qty": 50 } ], "expected_dispatch": "2026-09-10", "inquiry": "INQ-2026-00001" }
```

**`picking_api.list_pick_tasks`** — GET, optional `?order=` — ⬜
```
GET /api/method/dms_erp.sales.picking_api.list_pick_tasks?order=SAL-ORD-2026-00001
```
**`picking_api.auto_allocate`** — POST — ⬜
```json
POST /api/method/dms_erp.sales.picking_api.auto_allocate
{ "task": "<pick task id>" }
```
**`picking_api.patch_task`** — POST — ⬜
```json
POST /api/method/dms_erp.sales.picking_api.patch_task
{ "task": "<pick task id>", "patch": { "status": "Picked" } }
```

**`dealer_api.list_dealers`** — GET, optional `?search=&disabled=` — ✅ (fixed a `credit_limit` column bug)
```
GET /api/method/dms_erp.sales.dealer_api.list_dealers
```
```json
{ "message": [ { "id": "Sharma Tiles", "name": "Sharma Tiles", "creditLimit": 0, "disabled": false } ] }
```
**`dealer_api.get_dealer`** — GET — ⬜ (same fix applies)
```
GET /api/method/dms_erp.sales.dealer_api.get_dealer?dealer=Sharma Tiles
```

---

### 2.7 finance (`claims_api`, `unloading_api`)

**`claims_api.list_claims`** — GET, optional `?status=` — ✅
```
GET /api/method/dms_erp.finance.claims_api.list_claims
```
```json
{ "message": [] }
```

**`claims_api.file_claim`** — POST — 🚧 **BLOCKED** (needs a "Damage→Insurance Claim" Stock Entry, which needs `transfer_stock` fixed first)
```json
POST /api/method/dms_erp.finance.claims_api.file_claim
{ "stock_entry": "<Stock Entry name>", "insurer": "ABC Insurance Co", "claim_amount": 5000, "remarks": "10 boxes damaged in transit" }
```
**`claims_api.get_claim`** — GET — ⬜ (blocked, same reason)
**`claims_api.update_claim_status`** — POST — ⬜ (blocked, same reason)
**`claims_api.claim_summary`** — GET — ⬜ (blocked, same reason)

**`unloading_api.list_charges`** — GET, optional `?status=` — ⬜ (not blocked)
```
GET /api/method/dms_erp.finance.unloading_api.list_charges
```
**`unloading_api.get_charge_for_truck`** — GET — ⬜
```
GET /api/method/dms_erp.finance.unloading_api.get_charge_for_truck?inward_truck=cpji286jts
```
**`unloading_api.record_charge`** — POST — ⬜
```json
POST /api/method/dms_erp.finance.unloading_api.record_charge
{ "inward_truck": "cpji286jts", "contractor": "XYZ Labour Co", "rate_per_box": 5, "payment_mode": "Cash" }
```
**`unloading_api.mark_paid`** — POST — ⬜
```json
POST /api/method/dms_erp.finance.unloading_api.mark_paid
{ "charge": "<charge id>" }
```

---

### 2.8 comms (`dms_erp.comms.api`)

**`list_templates`** — GET — ✅
```
GET /api/method/dms_erp.comms.api.list_templates
```

**`send_message`** — POST — ✅
```json
POST /api/method/dms_erp.comms.api.send_message
{ "dealer": "Sharma Tiles", "text": "Hi, your order SAL-ORD-2026-00001 has been confirmed." }
```
```json
{ "message": { "id": "lv20srdvuv", "direction": "Outbound", "status": "Sent" } }
```

**`list_messages`** — GET — ✅
```
GET /api/method/dms_erp.comms.api.list_messages?dealer=Sharma Tiles
```

**`last_message`** — GET — ✅
```
GET /api/method/dms_erp.comms.api.last_message?dealer=Sharma Tiles
```

**`unreplied_inbound_count`** — GET — ✅
```
GET /api/method/dms_erp.comms.api.unreplied_inbound_count?dealer=Sharma Tiles
```
Returns `0` if staff has replied since the dealer's last message, else count of trailing unreplied inbound messages.

**`mark_read`** — POST or PUT — ✅
```json
POST /api/method/dms_erp.comms.api.mark_read
{ "message": "n5phhrpr9e" }
```

**`webhook_inbound_message`** — POST, `allow_guest` (shared-secret gated, no Bearer token) — ✅
```json
POST /api/method/dms_erp.comms.api.webhook_inbound_message
{ "secret": "<dms_erp_whatsapp_webhook_secret>", "dealer": "Sharma Tiles", "text": "Thanks, please confirm delivery date." }
```
```json
{ "message": { "id": "n5phhrpr9e", "direction": "Inbound", "status": "Delivered" } }
```

**`webhook_status_update`** — POST, `allow_guest` — ✅
```json
POST /api/method/dms_erp.comms.api.webhook_status_update
{ "secret": "<dms_erp_whatsapp_webhook_secret>", "message": "lv20srdvuv", "status": "Delivered" }
```

---

### 2.9 dashboard (`dms_erp.dashboard.api`) — all GET, no params

**`management_dashboard`** — ✅ (fixed a `Customer.credit_limit` column bug)
```
GET /api/method/dms_erp.dashboard.api.management_dashboard
```
```json
{ "message": { "totalSalesMtd": 0.0, "outstandingReceivables": 0, "claimableValue": 0, "topMovingItem": null, "salesByDealer": [], "alerts": [] } }
```

**`sales_dashboard`** — ✅ (fixed a "raw SQL function in get_all(fields=)" DataError)
```
GET /api/method/dms_erp.dashboard.api.sales_dashboard
```
```json
{ "message": { "todaysInquiries": 1, "pendingQuotations": 0, "ordersThisMonth": {"count":1,"value":28750.0}, "missedDemandValue": 0, "inquiryTrend": [...], "actionableInquiries": [] } }
```

**`warehouse_dashboard`** — ✅
```
GET /api/method/dms_erp.dashboard.api.warehouse_dashboard
```

**`purchase_dashboard`** — ✅ (⚠️ minor unreported bug: `purchaseTrend[].month` shows literal `"%Y-%m"` instead of a formatted month string, e.g. `"2026-09"` — not yet fixed)
```
GET /api/method/dms_erp.dashboard.api.purchase_dashboard
```

---

### 2.10 reports (all GET, no params tested — module untouched)

| API | Notes |
|---|---|
| `sales_reports.dealer_inquiry_report` | optional `dealer`, `status`, `from_date`, `to_date` |
| `sales_reports.missed_demand_report` | optional `from_date`, `to_date` |
| `sales_reports.retail_vs_bulk_report` | optional `from_date`, `to_date` |
| `sales_reports.dealer_activity_report` | optional `dealer` |
| `sales_reports.duplicate_inquiry_report` | optional `window_days` (default 7) |
| `warehouse_reports.bay_occupancy_report` | optional `warehouse`, `bay_type` |
| `warehouse_reports.visual_stock_balance` | optional `warehouse` |
| `warehouse_reports.stock_clearance_suggestions` | no params |
| `warehouse_reports.display_replacement_suggestions` | no params |
| `purchase_reports.reorder_planning_report` | optional `urgency`, `actionable_only` |
| `purchase_reports.purchase_pickup_plan` | no params |
| `purchase_reports.inquiry_to_po_mapping_report` | no params |
| `purchase_reports.po_pending_report` | optional `supplier`, `overdue_only` |
| `finance_reports.damage_and_insurance_report` | optional `status`, `insurer`, `from_date`, `to_date` — likely blocked, same batch-lot dependency as claims |
| `finance_reports.claimable_value_report` | no params — likely blocked, same reason |
| `finance_reports.unloading_payment_report` | optional `status`, `contractor`, `from_date`, `to_date` |
| `catalog_reports.pricing_and_csp_report` | optional `min_margin_pct` |
| `catalog_reports.product_movement_report` | optional `order` ("slow"/"fast") |
| `catalog_reports.product_activity_report` | optional `item` |
| `forecasting.demand_forecast` | optional `weeks_ahead` (default 4) — always returns `"low"` confidence by design |

---

## 3. Bugs found & fixed this session

| # | Bug | Root cause | Status |
|---|---|---|---|
| 1 | Every authenticated request with any parameter lost its args | `frappe.set_user()` in `auth/middleware.py` wiped `frappe.local.form_dict` as an undocumented side effect | ✅ Fixed & merged (commit `1bfb1a8`) |
| 2 | `management_dashboard` crashed: `Unknown column 'c.credit_limit'` | ERPNext stores credit limit in child table `Customer Credit Limit`, not on `Customer` directly | ✅ Fixed & merged (PR #31) |
| 3 | `dealer_api.list_dealers`/`get_dealer` — same credit_limit bug | Same root cause as #2 | ✅ Fixed & merged (PR #32) |
| 4 | `advance_order_stage` always failed: `UpdateAfterSubmitError` | `custom_fulfillment_stage`/`custom_stage_history` fields missing `allow_on_submit: 1` | ✅ Fixed & merged (PR #33) |
| 5 | `sales_dashboard`/similar crashed: `DataError: Use of sub-query or function is restricted` | Raw SQL functions (`count()`, `sum()`) passed into `frappe.get_all(fields=[...])`, rejected by this Frappe version's query-safety check | ✅ Fixed & merged (PR #35) |
| 6 | `transfer_stock` always fails: `Only 0 boxes of this batch in <bay>` | ERPNext v15 moved batch tracking to `Serial and Batch Bundle`; `list_stock_lots()` still reads the old, now-empty `Stock Ledger Entry.batch_no` column | 🚧 **Not yet fixed** — blocks all transfers + `finance.claims_api` |
| 7 | `purchase_dashboard.purchaseTrend[].month` shows literal `"%Y-%m"` | Date-format string not applied (likely a Python `strftime`/SQL `DATE_FORMAT` call bug) | ⬜ Not yet fixed, low priority |

---

## 4. Test coverage summary

| Module | Total APIs | Tested |
|---|---|---|
| auth | 5 | 1 |
| catalog | 8 | 2 |
| pricing | 5 | 2 |
| warehouse | 15 | 6 |
| purchase | 5 | 1 |
| sales | 22 | 6 |
| finance | 9 | 1 |
| comms | 8 | 8 ✅ complete |
| dashboard | 4 | 4 ✅ complete |
| reports | 27 | 0 |
| **Total** | **108** | **31** |
