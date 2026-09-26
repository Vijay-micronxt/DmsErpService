# Pacific Inc DMS — QA Use Case & Test Case Guide

This is a manual QA reference covering everything actually built so far, across both the
backend (`DmsErpService` — this repo) and the two frontends (`pacific-tileflow`, the
internal staff web app; `dms-dealer-portal`, the dealer self-service web app) plus the
WhatsApp Dealer Portal Flow. It is organized by BRD module (`docs/BRD.md`, Part C), and
for each module gives:

- **Use cases** — who does what, why, and what should happen.
- **Test cases** — concrete steps and expected results, numbered for tracking.

Only **built** functionality gets test cases. Where a BRD sub-section isn't built yet, it's
named under "Known gaps — not yet built" at the end of its module section, so QA doesn't
spend time testing something that doesn't exist. This guide reflects the system as of this
document's last update — re-check `pacific-tileflow/TODO.md` (the living build-status
backlog) if a section here looks stale.

## Systems in scope

| System | Repo | What it is |
|---|---|---|
| Backend | `DmsErpService` | Frappe/ERPNext app (`dms_erp`) — all business logic and data. |
| Staff web app | `pacific-tileflow` | Internal ERP UI for Sales/Warehouse/Purchase/Finance/Management roles. |
| Dealer web portal | `dms-dealer-portal` | Dealer-facing self-service app (BRD C.13) — phone+OTP login. |
| WhatsApp | whats91 "Dealer Portal" Flow + `dms_erp/comms/flow_api.py` | Automated dealer-facing WhatsApp menu (BRD C.2.2). |

## Roles referenced in test cases

Per BRD A.3: **Sales/CRM**, **Warehouse**, **Purchase**, **Finance**, **Management**
(owner/delegate), plus **Dealer** (external, via WhatsApp or the dealer portal). A test
case's precondition names the role that should be signed in.

## General preconditions for the whole guide

- A test ERPNext site with `dms_erp` installed, at least one user per staff role above.
- At least one test **Supplier**, one test **Dealer** (`Customer`), one **Product Series**,
  and a few **Items** under it, set up via Master Data test cases first (§1) — most other
  modules assume these already exist.
- For WhatsApp test cases: a real WhatsApp number registered against the test dealer's
  `Customer.custom_phone`, and the whats91 Dealer Portal Flow live and pointed at this site.

---

## 1. Master Data Module (BRD C.1)

### 1.1 Series-Driven Item Model (C.1.1) & Item Attribute Dictionary (C.1.2)

**Use case:** Purchase/Management creates a **Product Series** once (supplier, size,
finish, pieces/box, sqft/box, weight/box, bulk/retail quantity thresholds), then creates
every item in that family against it — the item inherits the series' attributes instead of
re-typing them each time. A size or thickness change means a *new* series, never a variant
on the same one.

**Where:** `pacific-tileflow` → Products (`/products`), the Series picker inside Add/Edit
Item.

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-MD-01 | Create a Series | Products → Add Item → use the `+` next to the Series picker → enter just a name → save. | A new Series is created with just that name; nothing else is required yet. |
| TC-MD-02 | New item inherits Series attributes | Add Item → pick an existing Series with supplier/size/finish/box-conversion already set → leave those fields blank on the item. | The item form fills in supplier, size, finish, pieces/box, sqft/box, weight/box from the Series automatically. |
| TC-MD-03 | Explicit item value overrides the Series | Same as above, but type a different finish before saving. | The typed value is kept; only the fields left blank are filled from the Series. |
| TC-MD-04 | Series label always mirrors the linked Series | Edit an item's Series link to point at a different Series. | The item's Series label display updates to the new Series' own name — it is never independently editable. |
| TC-MD-05 | Item without a Series (legacy data) | Open an item created before Series existed. | Shows "No Series master" — not treated as an error, and not retroactively enforced. |

### 1.2 UOM & Weight Handling (C.1.3)

**Use case:** Every item conversion (Box ↔ Pieces ↔ Sqft ↔ Sqm ↔ Weight) is consistent, and
weight appears on every relevant document.

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-MD-06 | Weight shown on product detail | Open a product with `weightPerBoxKg` set. | Weight per box is visible in the item detail panel. |
| TC-MD-07 | Batch weight can differ from the item's standard weight | Put away a batch during Inward/putaway with a different weight than the item's standard. | The batch stores its own actual weight; the item's standard weight is unaffected and used only as the default for future batches. |

### 1.3 Dealer / Customer Master (C.1.4)

**Use case:** Dealers are classified by price tier (Standard/Dealer/Master Dealer, driven by
their sales) and by dealer type (Retail/Bulk/Project/Distributor), which together drive
pricing and retail-vs-bulk defaults elsewhere in the system.

**Where:** `pacific-tileflow` → Dealers (`/dealers`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-MD-08 | Create a dealer with both classifications | Dealers → New → set dealer type (Retail/Bulk/Project/Distributor), price-visibility flag, salesperson. | Dealer is created and appears in every screen that lists dealers (Inquiries, Quotations, Orders, WhatsApp). |
| TC-MD-09 | Price-tier reclassification is automatic | Confirm several Sales Orders for a dealer to push their trailing sales past ₹1 lakh / ₹15 lakh. | The nightly `recompute_dealer_classifications` job (see `hooks.py`) reclassifies the dealer to Dealer / Master Dealer without manual action. |
| TC-MD-10 | Dealer type drives retail/bulk default | Create an Inquiry/Order for a Bulk or Project dealer with a small quantity. | The order defaults to bulk classification regardless of quantity (per C.4.3). |

### 1.4 Dealer Codes & Dealer Catalog Visibility (C.1.5)

**Use case:** A dealer can look up an item by their own private code *or* Pacific's company
code, but can only ever see/order items explicitly assigned to them — the actual visibility
boundary is the **Dealer Catalog** assignment (BRD's own "sample-issued" framing is
implemented here as an explicit per-dealer catalog assignment, not literally gated on a
Sample Request record).

**Where:** `pacific-tileflow` → Dealer Catalogs (`/dealer-catalogs`); item-level dealer
codes live on the Products page's item detail panel.

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-MD-11 | Assign an item to a dealer's catalog | Dealer Catalogs → pick a dealer → toggle a single product on. | That dealer can now see/inquire/quote/order that item everywhere (Inquiries, Quotations, WhatsApp). |
| TC-MD-12 | Category-level bulk toggle | Dealer Catalogs → pick a dealer → "Show all" / "Hide all" for a whole category. | Every product in that category flips visibility for that dealer in one action. |
| TC-MD-13 | Dealer with no assignment falls back to full catalog | Pick a brand-new dealer with no Dealer Catalog record at all. | They can see the entire sellable catalog — the fallback is full access, not zero access, until someone explicitly narrows it. |
| TC-MD-14 | Catalog boundary enforced in Inquiries/Quotations | As a dealer with a narrow catalog (e.g. 3 of 8 products), open New Inquiry / the Quotation Builder's item picker. | Only their 3 assigned products are selectable — the rest don't appear at all. |
| TC-MD-15 | Private dealer code resolves to the right item | Set a dealer-specific code on an item (Products → item detail → Dealer codes) → search/inquire using that code. | Resolves to the correct internal item. |
| TC-MD-16 | Item Dealer Code ≠ Dealer Catalog | Give a dealer a private code for an item they are **not** assigned in Dealer Catalogs. | The item still cannot be seen/ordered by that dealer — a private code is a shorthand layered on top of catalog visibility, not a visibility grant by itself. |

### 1.5 Supplier Master (C.1.6) & Transporter/Vehicle Master (C.1.7)

**Where:** `pacific-tileflow` → Suppliers (`/suppliers`), Transporters (`/transporters`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-MD-17 | Create a supplier with GPS + insurance holder | Suppliers → New → fill factory address, GPS lat/long, insurance holder, material-ready contact. | Saved; GPS feeds pickup planning (§5), insurance holder feeds claims (§8). |
| TC-MD-18 | Create a transporter with a vehicle | Transporters → New → Add Vehicle → set owner, vehicle number, type, capacity. | Vehicle appears under that transporter, available for pickup-run planning. |
| TC-MD-19 | Update / remove a vehicle | Transporters → edit an existing vehicle's capacity, or remove it. | Change reflected immediately; removed vehicle no longer selectable for new pickup runs. |

### 1.6 Warehouse & Bay Master (C.1.8)

**Where:** `pacific-tileflow` → Bay Master (`/bay-master`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-MD-20 | Create a bay | Bay Master → New Bay → set code, type (Main/Buffer/Damage/Insurance Claim/Display/Blocked), size, capacity, suitable category. | Bay appears in the Visual Warehouse Map (`/bays`) with correct type color-coding. |
| TC-MD-21 | Quick-create a grid of bays | Bay Master → Quick Create Grid → specify a naming pattern and count. | Multiple bays are created in one action, all following the given pattern. |
| TC-MD-22 | Link a Buffer bay to its Main bay | Create/edit a Buffer-type bay → set its linked main bay. | The link is used later by Buffer Bay Management's suggested-destination logic (§6). |

---

## 2. Dealer Inquiry & CRM + WhatsApp (BRD C.2)

### 2.1 Inquiry Capture (C.2.1)

**Use case:** Every dealer stock check or request — however it arrives (staff-entered,
WhatsApp, dealer portal) — becomes a structured Inquiry with a real status, not a phone
note that gets lost.

**Where:** `pacific-tileflow` → Inquiries (`/inquiries`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-CRM-01 | Log a new inquiry | New Inquiry → pick dealer → pick item (filtered to their catalog) → enter quantity and source (Phone/WhatsApp/Internal/Other). | Inquiry created; right-hand panel shows live stock, batches, alternatives. |
| TC-CRM-02 | Status derives from stock at creation | Log an inquiry for an item with zero stock. | Inquiry status is automatically Out of Stock (or Pre-order Required), not left as a generic Open needing a manual update. |
| TC-CRM-03 | Duplicate inquiry warning | Log two inquiries for the same dealer + item + similar quantity within the configured window. | The second logs successfully but shows a duplicate warning toast — a heads-up, not a block. |
| TC-CRM-04 | Empty-catalog dealer doesn't dead-end | Start New Inquiry for a dealer whose Dealer Catalog assignment is empty. | The flow still lets you proceed (falls back sanely) rather than showing a dead end with nothing selectable. |

### 2.2 WhatsApp Inquiry Flow (C.2.2) — automated menu

**Use case:** A dealer messages the WhatsApp number directly and self-serves routine
questions through a menu, with every exchange still landing in the same Communications
thread a staff member would see.

**Where:** Real WhatsApp conversation with the business number; results visible in
`pacific-tileflow` → Communications (`/communications`) and Inquiries.

**Precondition for every test case below:** the test dealer's WhatsApp number matches
`Customer.custom_phone` exactly, or nothing resolves.

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-WA-01 | Menu appears | Message the business WhatsApp number. | Get the 5-option menu: Item Availability & Price / Delivery Status / Order Status / Payment Due / Recent 5 Orders. |
| TC-WA-02 | In-stock item lookup | Pick option 1 → type a real item code in the test dealer's catalog with stock. | Reply shows size/finish, stock count, and price (if `custom_price_visible` is on for that dealer). |
| TC-WA-03 | Price hidden when dealer's account has it off | Same as above, dealer with `custom_price_visible` off. | No price line at all — never a placeholder or zero. |
| TC-WA-04 | Out-of-stock item with lead time + alternative | Look up an item with zero stock, `Supplier lead time (days)` set, and a real, catalog-visible, in-stock Alternative Item. | Reply states out of stock, expected lead time in days, and "You may also consider …" naming the real alternative. |
| TC-WA-05 | Batch-specific reply — single batch | Type an item code plus a quantity its largest single batch covers, e.g. `"105107 20 boxes"`. | Reply names that one batch specifically, not a flat total. |
| TC-WA-06 | Batch-specific reply — combination | Same, with a quantity no single batch covers (item split across 2–3 batches). | Reply lists the batch combination and the combined total. |
| TC-WA-07 | Batch shortfall | Ask for a quantity larger than every batch combined. | Reply reports the shortfall, not a broken/empty reply. |
| TC-WA-08 | No quantity given | Type just the item code/name, no quantity, for a multi-batch item. | Reply is the flat total across all batches (unchanged from before batch-suggestion existed). |
| TC-WA-09 | Multi-item message | Type several item codes/names in one message, comma/"and"/Hindi "और"-separated. | One reply line per item, each independently resolved. |
| TC-WA-10 | Fuzzy/typo name match | Type a slightly misspelled item name, no exact code. | Still resolves via the fuzzy fallback (confidence ≥ ~85%, no close competing item). |
| TC-WA-11 | Hindi/Hinglish sentence | Type the item name embedded in a full Hindi/Hinglish sentence. | Still resolves correctly. |
| TC-WA-12 | Confirm, don't guess — tied candidates | With two similarly-named items in the dealer's catalog (e.g. two finishes of one Series), type just the shared part of the name. | Reply lists **both** candidates by name and code and asks to retype the exact one — never silently picks one. |
| TC-WA-13 | Confirm, don't guess — weak single match | Type a badly garbled version of a real item name, no other close item exists. | Reply still asks to confirm rather than silently resolving. |
| TC-WA-14 | Every check raises a real Inquiry | After any successful lookup above. | A new Inquiry (source WhatsApp) appears in Inquiries — this is the same missed-demand signal Purchase Requirements reads. |
| TC-WA-15 | Request More Info | After a lookup, tap "🔔 Request More Info", type a note. | The Inquiry is raised/updated carrying the dealer's typed note. |
| TC-WA-16 | Place Order — end to end | After an in-stock lookup, tap "🛒 Place Order", choose a quantity band, enter a PO number when asked. | A real Sales Order is created carrying that PO number; WhatsApp reply names the new order number; the **correct item** is the one just looked up (regression check — this was a real production bug). |
| TC-WA-17 | Place Order with no resolved item is safe | Trigger an ambiguous lookup (TC-WA-12) then tap "🛒 Place Order" anyway without retyping. | Replies "we couldn't tell which item this enquiry is for, please start again" — never silently places an order for the wrong candidate. |
| TC-WA-18 | Delivery/Order Status — own order | Pick option 2 or 3 → enter a real Sales Order number belonging to the test dealer. | Correct current fulfillment stage (and dispatch/delivery date once dispatched) is shown. |
| TC-WA-19 | Delivery/Order Status — cross-dealer security | Enter a Sales Order number belonging to a **different** dealer. | Comes back "not found" — never reveals the other dealer's order or its stage. |
| TC-WA-20 | Payment Due | Pick option 4, dealer with a real outstanding balance, then a dealer with zero. | Correct amount shown; zero case shows "no outstanding dues." |
| TC-WA-21 | Recent 5 Orders | Pick option 5, dealer with several orders, then a dealer with none. | Up to 5 most recent orders with correct stages; "no orders yet" for the empty case. |
| TC-WA-22 | Diagnosing "nothing arrived" | If a real WhatsApp message produces nothing in Communications. | `/app/whats91-webhook-log` will show **nothing** (menu Flow calls ERPNext directly, bypassing that log) — check `/app/error-log` for a failed call instead, then whats91's own Flow setup if there's nothing there either. |

### 2.3 Inquiry Status & Closure (C.2.3), Duplicate Detection (C.2.4), Missed Demand (C.2.5)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-CRM-05 | Full status lifecycle | Move an inquiry through Open → Available/Partially Available/Out of Stock/Pre-order Required → Quoted → Converted to Order. | Each transition is reflected and reportable. |
| TC-CRM-06 | Closure requires a customer PO | Try to close an inquiry with no customer PO linked. | Cannot be marked fully closed — closure is gated on a linked PO number (BRD C.2.3). |
| TC-CRM-07 | Dropped inquiry records a reason | Reject/close an inquiry without converting it. | A reason is captured — feeds the missed-opportunity record. |
| TC-CRM-08 | Missed Demand report | Log several out-of-stock inquiries for the same item from different dealers. | Item surfaces in the Missed Demand report (`/reports` → Sales) and as a signal on the reorder plan. |
| TC-CRM-09 | Duplicate Inquiry report | With the duplicate scenario from TC-CRM-03 already logged. | Shows up in the Duplicate Inquiry report, not just the inline warning toast. |

### 2.4 Follow-up via WhatsApp (C.2.6) — Known gap, not yet built

No proactive follow-up reminders exist yet (`[Still need] [Received elsewhere] [Cancel]`
after a no-response window) — for either dealer follow-up or staff CRM reminders. This is
deliberately held off until whats91's actual template/button-reply API is confirmed against
real traffic (see `docs/whatsapp-flow-testing-todo.md`). **No test cases** — do not report
its absence as a bug.

---

## 3. Sales & Order Management (BRD C.3)

### 3.1 Quotations

**Where:** `pacific-tileflow` → Quotations (`/quotations`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-SO-01 | Build a quotation | Quotations → New → pick dealer → Add Item (filtered to their catalog, excludes unsellable items). | Quotation builds with correct dealer-scoped item picker. |
| TC-SO-02 | Retail markup applies automatically | Build a quotation for a Retail dealer. | Retail markup (`Dealer Price + X%`) is applied per the BRD's quotation pricing rule, without manual calculation. |
| TC-SO-03 | Convert quotation to order | Confirm a quotation. | A Sales Order is created, correctly carrying quotation line items/prices forward. |

### 3.2 Order Lifecycle & Reservation (C.3.1), Dispatch Control (C.3.4)

**Where:** `pacific-tileflow` → Orders (`/orders`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-SO-04 | Create an order with a mandatory delivery date | Orders → New → try to save with no delivery date. | Blocked — delivery date is mandatory. |
| TC-SO-05 | Stage lifecycle | Advance an order Confirmed → Picking → Ready to Dispatch → Dispatched → Delivered. | Each stage transition is tracked (`custom_fulfillment_stage` + history), visible consistently across Orders, WhatsApp status checks, and Recent Orders. |
| TC-SO-06 | Cancellation before dispatch | Cancel an order before it reaches Dispatched. | Allowed; blocked once past that point (see Approvals gap note below for whether this is enforced with a formal approval routing — currently a straightforward status transition, not a workflow-gated one). |
| TC-SO-07 | Dispatch payment lock | Attempt to dispatch an order for a dealer under an advance-payment condition, with the required advance not yet confirmed. | Dispatch is blocked (interim manual checkbox lock) until the advance is marked confirmed. |
| TC-SO-08 | Order traces back to its source | Open an order that originated from an Inquiry or Quotation. | The originating Inquiry/Quotation reference is visible on the order detail. |

### 3.3 Orders created directly by the dealer (WhatsApp / Dealer Portal)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-SO-09 | Dealer portal enquiry → order with own PO number | In `dms-dealer-portal`, convert an in-stock enquiry to an order, entering the dealer's own PO number. | Real Sales Order created, carrying that PO number, no staff step. |
| TC-SO-10 | WhatsApp Place Order (see TC-WA-16) | — | Same outcome as TC-SO-09, via WhatsApp instead. |

---

## 4. Purchase & Reorder Planning (BRD C.4)

**Use case:** The purchase team reviews an **auto-generated** reorder plan (they don't type
suggested quantities) and raises real POs from it.

**Where:** `pacific-tileflow` → Purchase Requirements (`/requirements`), Purchase Orders
(`/purchase-orders`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-PUR-01 | Reorder suggestion appears for a genuinely short item | Have an item with zero/low stock and real open retail demand (missed-demand inquiries + pending inquiries). | Item surfaces as Critical/urgent on `/requirements` with a computed suggested quantity, stock, missed demand, pending inquiries, and reason breakdown all shown. |
| TC-PUR-02 | Bulk/project demand excluded | Log a large Bulk-dealer order for an item. | This demand does **not** inflate that item's reorder suggestion — retail/sub-dealer channel only, per C.4.3. |
| TC-PUR-03 | Discontinued items excluded | An item with real missed demand but status Factory Discontinued or Pulled Back. | Excluded from reorder suggestions, with the reason stated as the actual status, not a generic "N/A". |
| TC-PUR-04 | Raise PO from a suggestion | On `/requirements`, click "Raise PO" for a suggested item. | A real Purchase Order is created, supplier/quantity prefilled from the suggestion, remarks auto-composed from the reason breakdown. |
| TC-PUR-05 | PO line progress tracking | Open a PO's detail (`/purchase-orders/$id`). | Ordered/ready/planned/received quantities shown per line, ready-qty editable inline. |
| TC-PUR-06 | Plan Inward from a PO | On a PO line with quantity still to plan, click "Plan Inward". | A real Inward Truck is created, linked back to the PO/line, appears immediately in `/inward`. |
| TC-PUR-07 | Reorder plan doesn't go stale | Raise a PO against a suggestion, then revisit `/requirements`. | The plan reflects the new PO's effect (in-transit/pending quantity), not the pre-PO numbers. |

### Known gaps — not yet built

- MOQ hierarchy enforcement and vendor-enquiry readiness capture (C.4.2) beyond what the
  reorder suggestion already surfaces.
- Purchase receipt shortage/claim capture and supplier invoice reference at receipt time
  (C.4.4) — `/inward` doesn't yet record a short/mismatched receipt against expectation.

---

## 5. Supplier Pickup & Route Planning (BRD C.5)

**Use case:** Purchase plans a pickup run against one supplier's ready POs.

**Where:** `pacific-tileflow` → Pickup Planner (`/pickup-planner`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-PICK-01 | Create a pickup run | Pickup Planner → New → pick vehicle type, vehicle number, scheduled date, driver, supplier. | Run created in Draft status. |
| TC-PICK-02 | Add line items from ready POs | Add one or more line items, each a specific item from that supplier's ready Purchase Orders. | Lines attach correctly; more can be added later. |
| TC-PICK-03 | Advance run status | Move the run Draft → Dispatched → Completed. | Each transition is tracked and visible on the run detail. |

### Known gaps — not yet built

Everything BRD asks for **beyond a single-supplier run**: a multi-supplier route across
several stops in one truck run, container-vs-truck auto-selection, Leaflet map/GPS pins,
drag-drop stop resequencing, "Auto-Generate Route" by proximity, live vehicle tracking
(C.5.1), a driver master, and "Share with Driver"/printable route sheet. Do not test these —
they don't exist yet.

---

## 6. Inventory: Batch, Bay, QR & Scanning (BRD C.6)

**Where:** `pacific-tileflow` → Bays (`/bays`), Allocations (`/allocations`), Buffer Bays
(`/buffer-bays`), Transfers (`/transfers`), Stock (`/stock`), Scan (`/scan`), Unallocated
Stock (`/unallocated-stock`).

### 6.1 Bay Allocation Workflow (C.6.3)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-WH-01 | System-suggested bay allocation | Allocations → New → pick item/quantity. | System suggests a suitable bay (matching category, checking capacity), highlighted on the map. |
| TC-WH-02 | Overflow falls back to a buffer bay | Suggest an allocation larger than the main bay's remaining capacity. | System falls back to a linked buffer bay (or a main+buffer split). |
| TC-WH-03 | Confirm/modify/split before printing | On the suggestion step, manually override the suggested bay or split the quantity. | Inline validation catches an invalid override (e.g. exceeding capacity); confirming produces a printable slip. |
| TC-WH-04 | Slip carries a real QR code | Print an allocation slip. | QR code renders correctly and scans back to the correct allocation. |
| TC-WH-05 | Occupancy recomputes after scan confirmation | Complete a putaway scan for a confirmed allocation. | Bay occupancy on the Visual Warehouse Map updates immediately. |

### 6.2 Buffer Bay Management (C.6.3 continued)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-WH-06 | Days-in-buffer aging | Have stock sitting in a Buffer bay for more than 7 days. | An alert banner appears; suggested main-bay destination is shown. |
| TC-WH-07 | One-click transfer to main | From the alert, transfer buffer stock to its suggested main bay. | Stock moves; buffer occupancy decreases, main bay occupancy increases. |
| TC-WH-08 | Buffer bay linked correctly to main | Create a buffer-to-main link (TC-MD-22) then trigger TC-WH-06/07. | The suggested destination matches the explicitly linked main bay, not an arbitrary one. |

### 6.3 Material Transfer (Transfers)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-WH-09 | Transfer between bays | Transfers → New → source/destination bay, item/batch, quantity, reason. | Validated (source actually holds the item/batch, destination has capacity) and recorded in transfer history. |
| TC-WH-10 | Damage-type transfer captures claim fields | Transfer with reason = damage. | Damage-type/claim-reference fields appear and are required. |

### 6.4 Visual Stock Balance (Stock)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-WH-11 | Map/List/Card views agree | Toggle between the three views for the same warehouse. | Same underlying stock, three presentations. |
| TC-WH-12 | Stock-age filter | Filter for stock older than 30/60 days. | Only qualifying lots appear. |
| TC-WH-13 | Top-3 batches shown, drill-down available | Open an item with more than 3 batches. | Top-3 by quantity shown by default; full batch list available on request (C.6.2). |

### 6.5 QR/Barcode Scanning (Scan)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-WH-14 | Inward placement scan — match | Scan mode: Inward Placement → scan item → scan the correct bay. | Confirms and updates stock/bay. |
| TC-WH-15 | Inward placement scan — mismatch | Same, but scan a different bay than assigned. | Alerts mismatch, blocks the action. |
| TC-WH-16 | Transfer scan | Scan mode: Transfer → source → item → destination → confirm. | Transfer recorded exactly as the manual Transfers flow would. |
| TC-WH-17 | Picking scan validates against the pick list | Scan mode: Picking, scan an item not on the active pick task. | Rejected — validated against the specific pick list, not accepted blindly. |
| TC-WH-18 | Bay audit scan | Scan mode: Bay Audit, count differs from expected. | Variance shown inline (no persistence of the audit session — by design, not a bug). |

### 6.6 Loose / Unallocated Stock (C.6.6)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-WH-19 | Record ad-hoc loose stock | Scan an item at a location that isn't an allocated bay. | Recorded as unallocated stock (item, batch, quantity, location); appears on the unallocated-stock report. |
| TC-WH-20 | Allocate loose stock to a proper bay | From the unallocated-stock report, allocate an entry to a real bay. | Standard Bay Allocation Slip flow runs; entry clears from "unallocated". |
| TC-WH-21 | Consolidate loose stock | From the report, consolidate a loose entry into existing open/partial stock of the same item/batch. | Quantities merge; no duplicate/orphaned stock record remains. |

### 6.7 Sticker printing (C.6.4)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-WH-22 | Print box/inward stickers | From a confirmed allocation, print box stickers. | Sticker shows item code, name, size/finish/series, batch (incl. manufacturing date), bay, boxes/pieces, weight, PR/supplier reference. |
| TC-WH-23 | Dealer sample sticker | Issue a sample (see §10). | Sticker carries a dealer-specific unique code, human-readable product name, series/size/finish, and the sample's batch. |

---

## 7. Pricing Engine (BRD C.7)

**Where:** `pacific-tileflow` → Pricing (`/pricing`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-PRC-01 | Series price setting / landing cost | Pricing → select a pending product → enter purchase/freight/handling/other costs and margin %. | Landing cost and suggested price compute live (`Landing Cost × (1 + margin%)`). |
| TC-PRC-02 | Approve & publish | Approve the computed price. | `dealerPrice` updates everywhere it's read (Stock, New Inquiry, Quotation Builder) immediately, plus a history entry is recorded. |
| TC-PRC-03 | Three price lists derive correctly | Check the published price against each dealer tier (Standard/Dealer/Master Dealer). | Each tier's rate is correctly derived — not all three showing the same flat number. |
| TC-PRC-04 | Quotation-level price rework doesn't touch the master list | Rework freight/margin/discount inside a single quotation. | The change is scoped to that quotation only; the published dealer price list is untouched. |
| TC-PRC-05 | Retail markup override | Apply a markup % on a retail quotation. | Applies only at the quotation level, never mutating the base dealer price. |
| TC-PRC-06 | Price/discount change requires approval framing | Change a price below the dealer's default list price. | Flagged as an override (see §11 for the current, partial approval-routing state). |

---

## 8. Damage & Insurance Claims (BRD C.8)

**Where:** `pacific-tileflow` → Damage (`/damage`), Claims (`/claims`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-DMG-01 | Identify damage | Damage → New → source bay, item/batch, quantity, damage type, photo, auto-suggested damage bay. | Damage record created; stock moves to the damage bay. |
| TC-DMG-02 | Damage → Insurance Claim transfer | From a damage record, transfer to Insurance Claim with insurer/amount. | Creates a real, structured `InsuranceClaim` (not free text) — Filed status. |
| TC-DMG-03 | Claim lifecycle | Advance a claim Filed → Approved → Settled (with its own settlement amount, which can differ from the claimed amount) or Rejected. | Each transition updates the claims ledger; receivable/settled totals recompute correctly. |
| TC-DMG-04 | Printable claim voucher | Open a filed/settled claim. | A printable voucher is available with insurer/amount/consignment references. |
| TC-DMG-05 | Damage screen links to Claims totals | Open `/damage`. | Banner shows live pending-receivable and settled totals, linking to `/claims`. |
| TC-DMG-06 | Shortage claim | File a claim from a receipt shortage (once receipt shortage capture exists — see §4 gap) with responsibility recorded (factory/driver/absorbed). | Same claim mechanism as transit/warehouse damage. *(Currently limited by the §4 receipt-shortage-capture gap — verify current behavior before assuming this is fully wired.)* |

---

## 9. Unloading & Labour Payments (BRD C.9)

**Where:** `pacific-tileflow` → Unloading (`/unloading`), Labour (`/labour`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-LAB-01 | Record an unloading charge | From an Inward truck card (past Scheduled), "Record unloading charge" → contractor, boxes, rate/box, payment mode. | Charge amount computes (`boxes × rate`), status Pending. |
| TC-LAB-02 | Mark unloading charge paid | Mark a Pending charge Paid. | `/unloading` and the originating `/inward` truck card both update live, no reload needed. |
| TC-LAB-03 | Printable unloading voucher | Open a recorded charge. | Voucher available for print. |
| TC-LAB-04 | Labour attendance entry | Labour → add an attendance day for a labourer (days/shift, work done). | Entry recorded; running dues update. |
| TC-LAB-05 | Record labour payment | Record a payment against a labourer's dues. | Payment mode/reference captured; outstanding dues reduce accordingly. |

---

## 10. Sample & Display Management (BRD C.10)

**Where:** `pacific-tileflow` → Samples Display (`/samples-display`); sample requests may
also be reachable from the Products/item detail panel.

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-SMP-01 | Request a sample | Create a Sample Request for a dealer + item. | Sits in a pending-approval list. |
| TC-SMP-02 | Approve a sample request | Approve the pending request. | Moves to approved, ready to issue. |
| TC-SMP-03 | Issue a sample | Issue the approved sample. | Real stock movement/invoice occurs; item leaves the pending list; a dealer-specific sample sticker (unique QR) is generated. |
| TC-SMP-04 | Auto-created Display Placement Slip | Immediately after issuing (TC-SMP-03). | A Display Placement Slip is auto-created (item, dealer/location, quantity, placement date, photo); its placement date starts the monitoring clock. |
| TC-SMP-05 | Monitoring reminder job | Placement date crosses a configured interval (3/6/12 months) — check via the scheduled job `send_display_monitoring_reminders`. | A WhatsApp/email reminder with response buttons is sent for that placement. |
| TC-SMP-06 | Reconciliation records the outcome | Respond to a monitoring reminder / manually record a reconciliation ([Still displayed]/[Removed]/[Loose, not shown]). | A Sample Display Reconciliation record is created/updated with that outcome. |
| TC-SMP-07 | Pullback on discontinuation | Pull back a display for a discontinued/removed item. | Stock returns to the warehouse with returned-condition tracked; feeds the resell/write-off/clearance decision. |
| TC-SMP-08 | Top Dealer identification | Check top-dealer logic against 6 months of retail sales, bulk/project excluded. | Correct dealers surface as "top" for sample/display planning. |
| TC-SMP-09 | Product withdrawal automation reaches display removal | Let an item's display duration/store-count/annual-sales thresholds breach per its Series' configured criteria. | The daily `evaluate_product_withdrawals` job advances its lifecycle status and auto-triggers the removal + put-up list and pullback, without manual action. |

---

## 11. Approvals & Workflow (BRD C.11) — Known gap, mostly not yet built

Per the BRD, six explicit override triggers require approval + notification: credit-limit
exceedance, overdue outstanding, any pricing/discount override, amendment/cancellation of a
submitted document, and audit-locked retail-vs-bulk override. **None of the formal
routing/notification workflow exists yet** — no sales-override approval routing, no
credit-limit/overdue controls beyond the numbers already shown on dashboards, no
mobile-approval flow. Submission does freeze documents in the underlying Frappe sense, but
there is no custom amend/cancel version-diff or owner-notification-on-critical-action layer
on top of that yet.

**No test cases** for the formal workflow — don't file a bug for its absence. Do verify,
opportunistically, that the underlying document submit/cancel primitives behave sanely
(e.g. TC-SO-06 above) since those are real ERPNext behavior, just not yet wrapped in the
BRD's approval routing.

---

## 12. Reporting, Dashboards & Assisted AI (BRD C.12)

### 12.1 Dashboards (C.12.1)

**Where:** `pacific-tileflow` → Dashboard (`/dashboard`), which branches by signed-in role.

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-RPT-01 | Role-appropriate dashboard | Sign in as each role (Sales/Warehouse/Purchase/Management). | Each sees the KPIs/alerts relevant to their function (inquiry activity; bay occupancy/pending allocations/damage; reorder/PO status; cross-functional summary). |
| TC-RPT-02 | Alert cards are live and clickable | Click a dashboard alert card (e.g. a pending allocation, damage awaiting claim, item below safety stock). | Navigates straight to the screen where you'd act on it. |
| TC-RPT-03 | Numbers aren't stale/cached | Compare a dashboard count against the underlying screen's live detail. | They agree — the dashboard recomputes each load, it doesn't cache. |

### 12.2 Report Set (C.12.2)

**Where:** `pacific-tileflow` → Reports (`/reports`), one flat searchable list across 6
categories: Sales, Warehouse, Purchase, Finance, Catalog, Forecasting.

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-RPT-04 | Every report loads with real data | Open each of the ~20 reports in turn (Inquiry register, Duplicate inquiry, Missed demand, Inquiry-to-PO mapping, Reorder planning, Purchase pickup plan, Bay occupancy, Visual stock balance, Consolidated/fragmented stock, Stock movement/ageing, Short stock, Retail vs bulk, Damage & claims, Unloading payment, Pricing & CSP, Fast/slow-moving product, Stock clearance, Display placement/ageing, Dealer/salesperson performance, Demand forecast). | Each loads without error and reflects real backend data — this module has **no mock fallback**, so an unconfigured backend shows an honest "no backend connected" state rather than fabricated numbers. |
| TC-RPT-05 | Filters narrow results correctly | Apply a filter (date range, dealer, item, category — whichever the report offers) on any report. | Result set narrows correctly; clearing the filter restores the full set. |
| TC-RPT-06 | CSV export | Export any report to CSV. | File downloads and matches what's shown on screen. |
| TC-RPT-07 | Click-to-sort | Click a sortable column header. | Table re-sorts client-side correctly. |
| TC-RPT-08 | Demand forecast chart | Open the Forecasting report. | Renders as a real chart (the one report with a chart; all others are tables). |

### 12.3 Salesperson Assignment (C.12.3)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-RPT-09 | Assign a salesperson to a dealer | Dealers → edit → set salesperson. | Assignment saved; appears in the Salesperson Assignment report. |

### Known gaps — not yet built

- Customer PO OCR & product matching (C.12.4) — no OCR/AI ingestion of customer POs.
- Assisted AI Wave 2 (C.12.5) — image search, AI recommendation, AI forecasting — explicitly
  out of scope for this phase per the BRD itself; do not test.
- Future Customer API Integration (C.12.6) — the BRD's own "interim approach" (dealer
  enters their own PO number to tag and close an order) **is** built (see TC-SO-09/TC-WA-16);
  a real dealer-ERP-to-Pacific API integration is not.

---

## 13. Web Dealer App — Enquiry Module (BRD C.13)

**Where:** `dms-dealer-portal` (separate app from the staff `pacific-tileflow`).

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-DP-01 | Phone + OTP login, no password | Open the dealer portal → enter the test dealer's phone number → enter the OTP received. | Logs in; session is scoped server-side to that dealer only. |
| TC-DP-02 | Catalog scoped to this dealer | Browse the catalog. | Only items in this dealer's Dealer Catalog assignment appear, each with images (product/application/additional) and price where enabled. |
| TC-DP-03 | Search by either code | Search using the dealer's own private code, then the company code, for the same item. | Both resolve to the same item detail page. |
| TC-DP-04 | Item detail shows stock/batches/price | Open an item's detail page. | Real-time stock, top-3 batches, and price (if enabled for this dealer) are shown. |
| TC-DP-05 | Enquiry → order (in stock) | Raise an enquiry on an in-stock item → convert to order, entering the dealer's own PO number. | Real Sales Order created, tagged with that PO number. |
| TC-DP-06 | Enquiry stays an enquiry (out of stock) | Raise an enquiry on an out-of-stock item. | Recorded as an inquiry/out-of-stock request; suggested alternatives shown, not silently dropped. |
| TC-DP-07 | Order/enquiry/dues history | Open Orders, Inquiries, Profile tabs. | Correct history and outstanding dues for this dealer only. |
| TC-DP-08 | Cross-dealer isolation | Attempt to access another dealer's order/inquiry by guessing an ID/URL. | Rejected — this dealer's session cannot read another dealer's data (mirrors the WhatsApp cross-dealer checks, TC-WA-19). |

---

## 14. Catalog & Price-List Formats for Dealers (BRD C.14)

| ID | Scenario | Steps | Expected Result |
|---|---|---|---|
| TC-CAT-01 | Dealer catalog export | Export a dealer-scoped catalog (`dealer_catalog_export`). | Contains only that dealer's visible items, with images and key attributes. |
| TC-CAT-02 | Price-inclusive vs price-exclusive | Export both variants for a dealer with pricing enabled. | Price-inclusive shows their tier's price/MRP; price-exclusive omits price entirely. |
| TC-CAT-03 | Dealer sees only their applicable price list | Compare exports for a Standard Dealer vs a Master Dealer on the same item. | Each shows their own tier's rate — never another tier's. |

---

## Appendix: Known gaps — not yet built (system-wide summary)

For quick reference — do not file these as bugs, and don't spend QA time hunting for them:

- **C.2.6** Proactive WhatsApp follow-up reminders (dealer or staff CRM) — genuinely new
  capability, deliberately blocked on confirming whats91's real template/button-reply API.
- **C.4.2/C.4.4** Full MOQ-hierarchy enforcement; purchase-receipt shortage/claim capture
  and supplier invoice reference at receipt time.
- **C.5/C.5.1** Multi-supplier route planning, map/GPS, drag-drop stops, live vehicle
  tracking, driver master, "Share with driver."
- **C.11** Formal approval/override routing, credit-limit/overdue enforcement, mobile
  approvals, amend/cancel version-diff.
- **C.12.4/C.12.5** Customer PO OCR, image-based AI search/recommendation/forecasting
  (Wave 2, explicitly deferred by the BRD itself).
- **Settings/Masters (BRD §13 Settings)** — no `/dealers`-adjacent Masters/Settings screens
  beyond what's covered above (e.g. no standalone Users/Masters admin screens for
  everything the nav implies).

If you find behavior that contradicts a "built" section above, that's a real regression —
file it. If you find a gap listed here behaving differently than "absent," that's worth a
note too, since it may mean partial work landed since this guide was last updated.
