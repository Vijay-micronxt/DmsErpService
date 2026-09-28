# Pacific Inc DMS — QA Use Case & Test Case Guide

This is a manual QA reference covering everything actually built so far, across both the
backend (`DmsErpService` — this repo) and the two frontends (`pacific-tileflow`, the
internal staff web app; `dms-dealer-portal`, the dealer self-service web app) plus the
WhatsApp Dealer Portal Flow. It is organized by BRD module (`docs/BRD.md`, Part C), and
for each module gives:

- **Use cases** — who does what, why, and what should happen.
- **Test cases** — concrete steps and expected results, each tagged with its **BRD Ref**
  and **Milestone / Wave** (per `docs/BRD.md` Part F.1/F.2), so QA can prioritize by
  delivery phase and trace every case back to the requirement it verifies.

Only **built** functionality gets test cases. Where a BRD sub-section isn't built yet, it's
named under "Known gaps — not yet built" at the end of its module section, so QA doesn't
spend time testing something that doesn't exist. This guide reflects the system as of this
document's last update — re-check `pacific-tileflow/TODO.md` (the living build-status
backlog) if a section here looks stale.

**On the Milestone/Wave column:** per BRD F.1, Milestone 1 is project setup (not
independently testable), Milestone 2 is core masters/inquiry/pricing/reorder, Milestone 3
is warehouse/pickup/damage/unloading/WhatsApp/dealer-app, Milestone 4 is
dashboards/assisted-AI/route-planning/UAT. Per BRD F.2, Wave 1 is the operational backbone
(everything above except assisted AI/route optimization), Wave 2 is assisted
intelligence. Where the BRD's own milestone/wave text doesn't explicitly name a module,
that's noted rather than guessed silently.

## Systems in scope

| System | Repo | What it is |
|---|---|---|
| Backend | `DmsErpService` | Frappe/ERPNext app (`dms_erp`) — all business logic and data. |
| Staff web app | `pacific-tileflow` | Internal ERP UI for Sales/Warehouse/Purchase/Finance/Management roles. |
| Dealer web portal | `dms-dealer-portal` | Dealer-facing self-service app (BRD C.13) — phone+OTP login. |
| WhatsApp | whats91 "Dealer Portal" Flow + `dms_erp/comms/flow_api.py`, plus the free-text path (`dms_erp/comms/api.py`, `intent.py`, `whats91.py`) | Automated dealer-facing WhatsApp menu (BRD C.2.2) and the separate LLM-classified free-text auto-reply path. |

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

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-MD-01 | Create a Series | Products → Add Item → use the `+` next to the Series picker → enter just a name → save. | A new Series is created with just that name; nothing else is required yet. | C.1.1 | M2 / Wave 1 |
| TC-MD-02 | New item inherits Series attributes | Add Item → pick an existing Series with supplier/size/finish/box-conversion already set → leave those fields blank on the item. | The item form fills in supplier, size, finish, pieces/box, sqft/box, weight/box from the Series automatically. | C.1.1 | M2 / Wave 1 |
| TC-MD-03 | Explicit item value overrides the Series | Same as above, but type a different finish before saving. | The typed value is kept; only the fields left blank are filled from the Series. | C.1.1 | M2 / Wave 1 |
| TC-MD-04 | Series label always mirrors the linked Series | Edit an item's Series link to point at a different Series. | The item's Series label display updates to the new Series' own name — it is never independently editable. | C.1.1 | M2 / Wave 1 |
| TC-MD-05 | Item without a Series (legacy data) | Open an item created before Series existed. | Shows "No Series master" — not treated as an error, and not retroactively enforced. | C.1.1 | M2 / Wave 1 |
| TC-MD-06 | Full attribute set on item creation | Add Item → set every attribute individually: Size, Finish (prime + sub-finish), Colour, Base Colour, Thickness, UOM, Pieces per Sq Ft, Weight per Box, Lead Time, Status. | All attributes save and display correctly on the item detail panel; Finish shows both the prime dropdown (Matt/Glossy) and its sub-finish. | C.1.2 | M2 / Wave 1 |
| TC-MD-07 | Image upload/remove | Products → item detail → Images → upload a Product, an Application/Room, and an Additional image. Then remove one. | Thumbnail grid shows all three with correct type badges; the removed image disappears from the grid and isn't served anywhere else (catalog export, dealer portal). | C.1.2 / C.14.1 | M2 / Wave 1 |

### 1.2 UOM & Weight Handling (C.1.3)

**Use case:** Every item conversion (Box ↔ Pieces ↔ Sqft ↔ Sqm ↔ Weight) is consistent, and
weight appears on every relevant document.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-MD-08 | Weight shown on product detail | Open a product with `weightPerBoxKg` set. | Weight per box is visible in the item detail panel. | C.1.3 | M2 / Wave 1 |
| TC-MD-09 | Batch weight can differ from the item's standard weight | Put away a batch during Inward/putaway with a different weight than the item's standard. | The batch stores its own actual weight; the item's standard weight is unaffected and used only as the default for future batches. | C.1.3 | M3 / Wave 1 |

### 1.3 Dealer / Customer Master (C.1.4)

**Use case:** Dealers are classified by price tier (Standard/Dealer/Master Dealer, driven by
their sales) and by dealer type (Retail/Bulk/Project/Distributor), which together drive
pricing and retail-vs-bulk defaults elsewhere in the system.

**Where:** `pacific-tileflow` → Dealers (`/dealers`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-MD-10 | Create a dealer with both classifications | Dealers → New → set dealer type (Retail/Bulk/Project/Distributor), price-visibility flag, salesperson. | Dealer is created and appears in every screen that lists dealers (Inquiries, Quotations, Orders, WhatsApp). | C.1.4 | M2 / Wave 1 |
| TC-MD-11 | Price-tier reclassification is automatic | Confirm several Sales Orders for a dealer to push their trailing sales past ₹1 lakh / ₹15 lakh. | The nightly `recompute_dealer_classifications` job (see `hooks.py`) reclassifies the dealer to Dealer / Master Dealer without manual action. | C.1.4 | M2 / Wave 1 |
| TC-MD-12 | Dealer type drives retail/bulk default | Create an Inquiry/Order for a Bulk or Project dealer with a small quantity. | The order defaults to bulk classification regardless of quantity (per C.4.3). | C.1.4 / C.4.3 | M2 / Wave 1 |
| TC-MD-13 | Out-of-station floor never lowers a dealer's tier | Reclassify an out-of-station dealer whose sales alone would already earn Master Dealer. | Stays at Master Dealer (the floor only ever raises a tier, never lowers one that's already earned on volume). | C.1.4 | M2 / Wave 1 |

### 1.4 Dealer Codes & Dealer Catalog Visibility (C.1.5)

**Use case:** A dealer can look up an item by their own private code *or* Pacific's company
code, but can only ever see/order items explicitly assigned to them — the actual visibility
boundary is the **Dealer Catalog** assignment (BRD's own "sample-issued" framing is
implemented here as an explicit per-dealer catalog assignment, not literally gated on a
Sample Request record).

**Where:** `pacific-tileflow` → Dealer Catalogs (`/dealer-catalogs`); item-level dealer
codes live on the Products page's item detail panel.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-MD-14 | Assign an item to a dealer's catalog | Dealer Catalogs → pick a dealer → toggle a single product on. | That dealer can now see/inquire/quote/order that item everywhere (Inquiries, Quotations, WhatsApp). | C.1.5 | M2 / Wave 1 |
| TC-MD-15 | Category-level bulk toggle | Dealer Catalogs → pick a dealer → "Show all" / "Hide all" for a whole category. | Every product in that category flips visibility for that dealer in one action. | C.1.5 | M2 / Wave 1 |
| TC-MD-16 | Category coverage indicator | Open Dealer Catalogs for a dealer with a mixed (partially-visible) category. | Coverage indicator shows the category as partial, not fully on/off. | C.1.5 | M2 / Wave 1 |
| TC-MD-17 | Dealer with no assignment falls back to full catalog | Pick a brand-new dealer with no Dealer Catalog record at all. | They can see the entire sellable catalog — the fallback is full access, not zero access, until someone explicitly narrows it. | C.1.5 | M2 / Wave 1 |
| TC-MD-18 | Catalog boundary enforced in Inquiries/Quotations | As a dealer with a narrow catalog (e.g. 3 of 8 products), open New Inquiry / the Quotation Builder's item picker. | Only their 3 assigned products are selectable — the rest don't appear at all. | C.1.5 | M2 / Wave 1 |
| TC-MD-19 | Private dealer code resolves to the right item | Set a dealer-specific code on an item (Products → item detail → Dealer codes) → search/inquire using that code. | Resolves to the correct internal item. | C.1.5 | M2 / Wave 1 |
| TC-MD-20 | Item Dealer Code ≠ Dealer Catalog | Give a dealer a private code for an item they are **not** assigned in Dealer Catalogs. | The item still cannot be seen/ordered by that dealer — a private code is a shorthand layered on top of catalog visibility, not a visibility grant by itself. | C.1.5 | M2 / Wave 1 |
| TC-MD-21 | Dealer catalog export | Export a dealer-scoped catalog (`dealer_catalog_export`) for a dealer with a narrow assignment. | Export contains only that dealer's visible items. | C.1.5 / C.14.1 | M2 / Wave 1 |

### 1.5 Supplier Master (C.1.6), Transporter/Vehicle Master (C.1.7) & Warehouse Groups

**Where:** `pacific-tileflow` → Suppliers (`/suppliers`), Transporters (`/transporters`),
Bay Master (`/bay-master`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-MD-22 | Create a supplier with GPS + insurance holder | Suppliers → New → fill factory address, GPS lat/long, insurance holder, material-ready contact. | Saved; GPS feeds pickup planning (§6), insurance holder feeds claims (§9). | C.1.6 | M2 / Wave 1 |
| TC-MD-23 | Create a transporter with a vehicle | Transporters → New → Add Vehicle → set owner, vehicle number, type, capacity. | Vehicle appears under that transporter, available for pickup-run planning. | C.1.7 | M3 / Wave 1 |
| TC-MD-24 | Update / remove a vehicle | Transporters → edit an existing vehicle's capacity, or remove it. | Change reflected immediately; removed vehicle no longer selectable for new pickup runs. | C.1.7 | M3 / Wave 1 |
| TC-MD-25 | Create a Warehouse Group | Bay Master → create a new Warehouse Group (a container above individual bays, for a site with more than one warehouse). | Group is created and selectable when creating/filtering bays under it. | C.1.8 | M3 / Wave 1 |

### 1.6 Warehouse & Bay Master (C.1.8)

**Where:** `pacific-tileflow` → Bay Master (`/bay-master`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-MD-26 | Create a bay | Bay Master → New Bay → set code, type (Main/Buffer/Damage/Insurance Claim/Display/Blocked), size, capacity, suitable category. | Bay appears in the Visual Warehouse Map (`/bays`) with correct type color-coding. | C.1.8 | M3 / Wave 1 |
| TC-MD-27 | Quick-create a grid of bays | Bay Master → Quick Create Grid → specify a naming pattern and count. | Multiple bays are created in one action, all following the given pattern. | C.1.8 | M3 / Wave 1 |
| TC-MD-28 | Link a Buffer bay to its Main bay | Create/edit a Buffer-type bay → set its linked main bay. | The link is used later by Buffer Bay Management's suggested-destination logic (§7). | C.1.8 | M3 / Wave 1 |

---

## 2. Authentication & Staff User Management

Not a numbered BRD functional module in its own right, but the access layer BRD A.3
(Users & Roles) and Part B.2 (middleware) assume everything else depends on. Covered here
once rather than repeated per module.

**Where:** Login screen of `pacific-tileflow`; Users (`/users`) for staff account
management.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-AUTH-01 | Staff login | Sign in with a valid staff username/password. | Logged in; session token issued; landing dashboard matches the user's role. | A.3 / B.2 | M1-M2 / Wave 1 |
| TC-AUTH-02 | Invalid credentials rejected | Sign in with a wrong password. | Rejected with a clear error; no session issued. | A.3 / B.2 | M1-M2 / Wave 1 |
| TC-AUTH-03 | Token refresh keeps a long session alive | Stay signed in past the access token's expiry (or force a refresh). | Session continues without forcing a re-login; a new access token is issued transparently. | B.2 | M2 / Wave 1 |
| TC-AUTH-04 | Logout ends the session | Log out. | Session token invalidated; further API calls with the old token are rejected. | B.2 | M2 / Wave 1 |
| TC-AUTH-05 | Logout-all revokes every device | Sign in on two browsers/devices, then use "logout all" from one. | Both sessions are invalidated, not just the one that issued the logout-all call. | B.2 | M2 / Wave 1 |
| TC-AUTH-06 | Current-user info (`me`) is correct | After login, check the profile/identity shown in the app header. | Matches the signed-in user's real name, role(s), and username — not stale or another user's. | A.3 | M2 / Wave 1 |
| TC-AUTH-07 | Create a staff user with a role | Users → New → set name, username, role (Sales/Warehouse/Purchase/Finance/Management). | New user can log in and sees exactly the screens their role grants. | A.3 | M1-M2 / Wave 1 |
| TC-AUTH-08 | Update a user's role | Users → edit an existing user → change their role. | Their access changes accordingly on next login (or immediately, if enforced live). | A.3 | M1-M2 / Wave 1 |
| TC-AUTH-09 | List/search users | Users → search/filter the user list. | Correct filtered results; no user outside this site's own users leaks in. | A.3 | M1-M2 / Wave 1 |

---

## 3. Role-Based Access Control (cross-cutting)

Not a single BRD sub-section — a negative-test pass across every module, verifying BRD
A.3's "menus and permissions are role-based" is actually enforced, not just reflected in
what the UI chooses to show.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-RBAC-01 | Sales cannot approve pricing | Sign in as Sales/CRM → attempt to reach Pricing's Approve & Publish action (via UI or a direct API call). | Blocked — pricing approval is Purchase/Management only. | A.3 / C.7 | M2 / Wave 1 |
| TC-RBAC-02 | Warehouse cannot create Purchase Orders | Sign in as Warehouse → attempt to create a PO. | Blocked — PO creation is Purchase/Management only. | A.3 / C.4 | M2 / Wave 1 |
| TC-RBAC-03 | Finance-only actions blocked for other roles | Sign in as Sales → attempt to record an unloading payment or settle a claim. | Blocked — these are Finance actions. | A.3 / C.8 / C.9 | M3 / Wave 1 |
| TC-RBAC-04 | Dealer session cannot reach staff endpoints | Using a dealer portal session token, attempt to call a staff-only API (e.g. list all dealers, approve a price). | Rejected — a dealer session is scoped server-side to that dealer's own data only, never staff functions. | B.2 / C.13 | M3 / Wave 1 |
| TC-RBAC-05 | A dealer's WhatsApp/portal session cannot reach another dealer's data | Covered in depth as TC-WA-19 and TC-DP-08 below — listed here for cross-reference. | Rejected in both channels identically. | B.2 | M3 / Wave 1 |

---

## 4. Dealer Inquiry & CRM + WhatsApp (BRD C.2)

### 4.1 Inquiry Capture (C.2.1)

**Use case:** Every dealer stock check or request — however it arrives (staff-entered,
WhatsApp, dealer portal) — becomes a structured Inquiry with a real status, not a phone
note that gets lost.

**Where:** `pacific-tileflow` → Inquiries (`/inquiries`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-CRM-01 | Log a new inquiry | New Inquiry → pick dealer → pick item (filtered to their catalog) → enter quantity and source (Phone/WhatsApp/Internal/Other). | Inquiry created; right-hand panel shows live stock, batches, alternatives. | C.2.1 | M2 / Wave 1 |
| TC-CRM-02 | Status derives from stock at creation | Log an inquiry for an item with zero stock. | Inquiry status is automatically Out of Stock (or Pre-order Required), not left as a generic Open needing a manual update. | C.2.1 / C.2.3 | M2 / Wave 1 |
| TC-CRM-03 | Duplicate inquiry warning | Log two inquiries for the same dealer + item + similar quantity within the configured window. | The second logs successfully but shows a duplicate warning toast — a heads-up, not a block. | C.2.4 | M2 / Wave 1 |
| TC-CRM-04 | Empty-catalog dealer doesn't dead-end | Start New Inquiry for a dealer whose Dealer Catalog assignment is empty. | The flow still lets you proceed (falls back sanely) rather than showing a dead end with nothing selectable. | C.2.1 / C.1.5 | M2 / Wave 1 |

### 4.2 Free-text WhatsApp auto-reply (separate from the menu Flow)

**Use case:** A dealer messaging the WhatsApp number with an ordinary, unprompted free-text
question ("stock hai kya GVT 6013?") — not going through the Dealer Portal Flow's menu at
all — still gets an automatic, LLM-classified reply for a stock/price question, logged the
same way as everything else in Communications.

**Where:** `dms_erp/comms/whats91.py` (`receive_webhook`) → `dms_erp/comms/intent.py` →
`dms_erp/comms/api.py` (`_maybe_auto_reply`). Visible in `pacific-tileflow` →
Communications.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-CRM-05 | Free-text stock/price question gets an auto-reply | Message the WhatsApp number with a free-text availability question outside the menu Flow (e.g. immediately after a prior conversation ended, not right after the keyword trigger). | The webhook classifies intent and replies with the item's stock/price, same as the menu path would, logged in Communications. | C.2.2 / B.2 | M3 / Wave 1 |
| TC-CRM-06 | Ambiguous/off-topic free text does not force a wrong reply | Send a free-text message that isn't clearly a stock/price question (e.g. "kal milte hain"). | No forced/guessed auto-reply is sent for something that isn't actually an availability question. | C.2.2 | M3 / Wave 1 |
| TC-CRM-07 | Auto-reply raises an Inquiry the same way the menu path does | After TC-CRM-05. | A real Inquiry (source WhatsApp) is created, same as the Flow-driven lookup does. | C.2.1 / C.2.2 | M3 / Wave 1 |
| TC-CRM-08 | Webhook log records this path | Check `/app/whats91-webhook-log` after TC-CRM-05. | Unlike the menu Flow (see TC-WA-22), this free-text path **does** go through `receive_webhook`, so it **is** recorded here — useful contrast to remember when diagnosing "nothing arrived." | B.2 | M3 / Wave 1 |

### 4.3 Manual Communications screen (staff-initiated messaging)

**Use case:** A staff member proactively messages a dealer — a template (item
available/price update/payment reminder) or a free-typed message — from the same screen
that shows the automated traffic.

**Where:** `pacific-tileflow` → Communications (`/communications`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-CRM-09 | Send a template message | Communications → pick a dealer → send an "Item available" template, prefilled from real item/stock/price context. | Message sends; thread updates immediately with a delivery status (Sent → Delivered). | C.2.2 / C.20 | M3 / Wave 1 |
| TC-CRM-10 | Send a free-text message | Communications → same dealer → compose and send a custom message. | Same send/delivery-status behavior as the template case. | C.2.2 / C.20 | M3 / Wave 1 |
| TC-CRM-11 | Unread indicator and mark-read | Have a dealer with an unreplied inbound message (red dot). Open their thread. | Red dot clears (mark-read); reappears only on a genuinely new inbound message. | C.2.2 | M3 / Wave 1 |

### 4.4 WhatsApp Inquiry Flow (C.2.2) — automated menu

**Use case:** A dealer messages the WhatsApp number directly and self-serves routine
questions through a menu, with every exchange still landing in the same Communications
thread a staff member would see.

**Where:** Real WhatsApp conversation with the business number; results visible in
`pacific-tileflow` → Communications (`/communications`) and Inquiries.

**Precondition for every test case below:** the test dealer's WhatsApp number matches
`Customer.custom_phone` exactly, or nothing resolves.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WA-01 | Menu appears | Message the business WhatsApp number. | Get the 5-option menu: Item Availability & Price / Delivery Status / Order Status / Payment Due / Recent 5 Orders. | C.2.2 | M3 / Wave 1 |
| TC-WA-02 | In-stock item lookup | Pick option 1 → type a real item code in the test dealer's catalog with stock. | Reply shows size/finish, stock count, and price (if `custom_price_visible` is on for that dealer). | C.2.2 | M3 / Wave 1 |
| TC-WA-03 | Price hidden when dealer's account has it off | Same as above, dealer with `custom_price_visible` off. | No price line at all — never a placeholder or zero. | C.2.2 / C.7.1 | M3 / Wave 1 |
| TC-WA-04 | Out-of-stock item with lead time + alternative | Look up an item with zero stock, `Supplier lead time (days)` set, and a real, catalog-visible, in-stock Alternative Item. | Reply states out of stock, expected lead time in days, and "You may also consider …" naming the real alternative. | C.2.2 | M3 / Wave 1 |
| TC-WA-05 | Batch-specific reply — single batch | Type an item code plus a quantity its largest single batch covers, e.g. `"105107 20 boxes"`. | Reply names that one batch specifically, not a flat total. | C.2.2 / C.6.2 | M3 / Wave 1 |
| TC-WA-06 | Batch-specific reply — combination | Same, with a quantity no single batch covers (item split across 2–3 batches). | Reply lists the batch combination and the combined total. | C.2.2 / C.6.2 | M3 / Wave 1 |
| TC-WA-07 | Batch shortfall | Ask for a quantity larger than every batch combined. | Reply reports the shortfall, not a broken/empty reply. | C.2.2 / C.6.2 | M3 / Wave 1 |
| TC-WA-08 | No quantity given | Type just the item code/name, no quantity, for a multi-batch item. | Reply is the flat total across all batches (unchanged from before batch-suggestion existed). | C.2.2 | M3 / Wave 1 |
| TC-WA-09 | Multi-item message | Type several item codes/names in one message, comma/"and"/Hindi "और"-separated. | One reply line per item, each independently resolved. | C.2.2 | M3 / Wave 1 |
| TC-WA-10 | Fuzzy/typo name match | Type a slightly misspelled item name, no exact code. | Still resolves via the fuzzy fallback (confidence ≥ ~85%, no close competing item). | C.2.1 / C.2.2 | M3 / Wave 1 |
| TC-WA-11 | Hindi/Hinglish sentence | Type the item name embedded in a full Hindi/Hinglish sentence. | Still resolves correctly. | C.2.2 | M3 / Wave 1 |
| TC-WA-12 | Confirm, don't guess — tied candidates | With two similarly-named items in the dealer's catalog (e.g. two finishes of one Series), type just the shared part of the name. | Reply lists **both** candidates by name and code and asks to retype the exact one — never silently picks one. | C.2.1 | M3 / Wave 1 |
| TC-WA-13 | Confirm, don't guess — weak single match | Type a badly garbled version of a real item name, no other close item exists. | Reply still asks to confirm rather than silently resolving. | C.2.1 | M3 / Wave 1 |
| TC-WA-14 | Every check raises a real Inquiry | After any successful lookup above. | A new Inquiry (source WhatsApp) appears in Inquiries — this is the same missed-demand signal Purchase Requirements reads. | C.2.1 / C.2.5 | M3 / Wave 1 |
| TC-WA-15 | Request More Info | After a lookup, tap "🔔 Request More Info", type a note. | The Inquiry is raised/updated carrying the dealer's typed note. | C.2.2 | M3 / Wave 1 |
| TC-WA-16 | Place Order — end to end | After an in-stock lookup, tap "🛒 Place Order", choose a quantity band, enter a PO number when asked. | A real Sales Order is created carrying that PO number; WhatsApp reply names the new order number; the **correct item** is the one just looked up (regression check — this was a real production bug). | C.2.2 / C.12.6 | M3 / Wave 1 |
| TC-WA-17 | Place Order with no resolved item is safe | Trigger an ambiguous lookup (TC-WA-12) then tap "🛒 Place Order" anyway without retyping. | Replies "we couldn't tell which item this enquiry is for, please start again" — never silently places an order for the wrong candidate. | C.2.1 / C.2.2 | M3 / Wave 1 |
| TC-WA-18 | Delivery/Order Status — own order | Pick option 2 or 3 → enter a real Sales Order number belonging to the test dealer. | Correct current fulfillment stage (and dispatch/delivery date once dispatched) is shown. | C.2.2 / C.3.1 | M3 / Wave 1 |
| TC-WA-19 | Delivery/Order Status — cross-dealer security | Enter a Sales Order number belonging to a **different** dealer. | Comes back "not found" — never reveals the other dealer's order or its stage. | C.2.2 / B.2 | M3 / Wave 1 |
| TC-WA-20 | Payment Due | Pick option 4, dealer with a real outstanding balance, then a dealer with zero. | Correct amount shown; zero case shows "no outstanding dues." | C.2.2 | M3 / Wave 1 |
| TC-WA-21 | Recent 5 Orders | Pick option 5, dealer with several orders, then a dealer with none. | Up to 5 most recent orders with correct stages; "no orders yet" for the empty case. | C.2.2 | M3 / Wave 1 |
| TC-WA-22 | Diagnosing "nothing arrived" | If a real WhatsApp message produces nothing in Communications. | `/app/whats91-webhook-log` will show **nothing** (menu Flow calls ERPNext directly, bypassing that log) — check `/app/error-log` for a failed call instead, then whats91's own Flow setup if there's nothing there either. | C.2.2 | M3 / Wave 1 |

### 4.5 Inquiry Status & Closure (C.2.3), Duplicate Detection (C.2.4), Missed Demand (C.2.5)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-CRM-12 | Full status lifecycle | Move an inquiry through Open → Available/Partially Available/Out of Stock/Pre-order Required → Quoted → Converted to Order. | Each transition is reflected and reportable. | C.2.3 | M2 / Wave 1 |
| TC-CRM-13 | Closure requires a customer PO | Try to close an inquiry with no customer PO linked. | Cannot be marked fully closed — closure is gated on a linked PO number (BRD C.2.3). | C.2.3 | M2 / Wave 1 |
| TC-CRM-14 | Dropped inquiry records a reason | Reject/close an inquiry without converting it. | A reason is captured — feeds the missed-opportunity record. | C.2.3 | M2 / Wave 1 |
| TC-CRM-15 | Missed Demand report | Log several out-of-stock inquiries for the same item from different dealers. | Item surfaces in the Missed Demand report (`/reports` → Sales) and as a signal on the reorder plan. | C.2.5 | M2 / Wave 1 |
| TC-CRM-16 | Duplicate Inquiry report | With the duplicate scenario from TC-CRM-03 already logged. | Shows up in the Duplicate Inquiry report, not just the inline warning toast. | C.2.4 | M2 / Wave 1 |

### 4.6 Follow-up via WhatsApp (C.2.6) — Known gap, not yet built

No proactive follow-up reminders exist yet (`[Still need] [Received elsewhere] [Cancel]`
after a no-response window) — for either dealer follow-up or staff CRM reminders. This is
deliberately held off until whats91's actual template/button-reply API is confirmed against
real traffic (see `docs/whatsapp-flow-testing-todo.md`). **No test cases** — do not report
its absence as a bug. *(BRD ref C.2.6, Milestone 3, Wave 1 — i.e. this is core Phase-1
scope that's simply not built yet, not a Wave-2 deferral.)*

---

## 5. Sales & Order Management (BRD C.3)

*BRD's own Milestone table (F.1) doesn't explicitly name "Sales & Order Management" under
any milestone — it's inferred here as Milestone 2 (the natural continuation of the
Inquiry workflow it says is in scope), Wave 1.*

### 5.1 Quotations

**Where:** `pacific-tileflow` → Quotations (`/quotations`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-SO-01 | Build a quotation | Quotations → New → pick dealer → Add Item (filtered to their catalog, excludes unsellable items). | Quotation builds with correct dealer-scoped item picker. | C.3.1 / C.7.2 | M2 / Wave 1 |
| TC-SO-02 | Retail markup applies automatically | Build a quotation for a Retail dealer. | Retail markup (`Dealer Price + X%`) is applied per the BRD's quotation pricing rule, without manual calculation. | C.7.2 | M2 / Wave 1 |
| TC-SO-03 | Convert quotation to order | Confirm a quotation. | A Sales Order is created, correctly carrying quotation line items/prices forward. | C.3.1 | M2 / Wave 1 |
| TC-SO-04 | Merge several inquiries into one quotation | Select more than one open Inquiry for the same dealer → build one Quotation from them. | All selected inquiries' items land on the one quotation; each inquiry's own status/reference updates accordingly. | C.2.3 / C.3.1 | M2 / Wave 1 |
| TC-SO-05 | Line discount and item delivery date | On a quotation line, apply a per-line discount and set a per-item delivery date. | Both save correctly and carry forward if converted to an order. | C.3.1 / C.7.3 | M2 / Wave 1 |

### 5.2 Order Lifecycle & Reservation (C.3.1), Dispatch Control (C.3.4)

**Where:** `pacific-tileflow` → Orders (`/orders`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-SO-06 | Create an order with a mandatory delivery date | Orders → New → try to save with no delivery date. | Blocked — delivery date is mandatory. | C.3.1 | M2 / Wave 1 |
| TC-SO-07 | Per-item delivery date on a multi-item order | Create a multi-line order, set a different delivery date per line. | Each line's own delivery date is respected independently. | C.3.1 | M2 / Wave 1 |
| TC-SO-08 | Stage lifecycle | Advance an order Confirmed → Picking → Ready to Dispatch → Dispatched → Delivered. | Each stage transition is tracked (`custom_fulfillment_stage` + history), visible consistently across Orders, WhatsApp status checks, and Recent Orders. | C.3.1 | M2 / Wave 1 |
| TC-SO-09 | Cancellation before dispatch | Cancel an order before it reaches Dispatched. | Allowed; blocked once past that point (Delivered/already-Cancelled). As Sales, the cancellation itself is now audit-locked — queued for Management approval, not applied immediately (BRD C.11 trigger #5, see §13 TC-APR-09); as Management, it applies immediately. | C.3.1 / C.11 | M2 / Wave 1 |
| TC-SO-10 | Dispatch payment lock (interim manual gate) | Attempt to dispatch an order for a dealer under an advance-payment condition, with the required advance not yet confirmed. | Dispatch is blocked until Management manually confirms the advance — this is an **interim manual checkbox**, not the real VALS API integration (that integration is not built; blocked on external banking credentials). | C.3.4 | M2 / Wave 1 |
| TC-SO-11 | Order traces back to its source | Open an order that originated from an Inquiry or Quotation. | The originating Inquiry/Quotation reference is visible on the order detail. | C.3.1 | M2 / Wave 1 |
| TC-SO-12 | Real GST display | Open an order/quotation with a tax template applied. | GST amount displayed is computed from the real tax template, not a placeholder. | C.3.1 | M2 / Wave 1 |

### 5.3 Orders created directly by the dealer (WhatsApp / Dealer Portal)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-SO-13 | Dealer portal enquiry → order with own PO number | In `dms-dealer-portal`, convert an in-stock enquiry to an order, entering the dealer's own PO number. | Real Sales Order created, carrying that PO number, no staff step. | C.13.1 / C.12.6 | M3 / Wave 1 |
| TC-SO-14 | WhatsApp Place Order (see TC-WA-16) | — | Same outcome as TC-SO-13, via WhatsApp instead. | C.2.2 / C.12.6 | M3 / Wave 1 |

### Known gaps — not yet built

- **C.3.2** Supplier vs customer timeline comparison / delay-impact escalation, and the
  stock-in-hand/in-transit/factory-dispatch forecasting window — not built.
- **C.3.3** Bill-to/Ship-to validation, drop-ship (Sales Order linked to a PO shipping
  direct to the customer), and automatic e-Way Bill generation — **confirmed not built at
  all** (no `drop_ship`/`bill_to`/`ship_to`/`eway` references anywhere in `sales/*.py`).
  Do not assume this is silently covered — it genuinely isn't.
- **C.3.4** The real India Banking / VALS API integration — only the interim manual
  advance-confirmation gate (TC-SO-10) exists; the receipt-triggered automatic release
  is not built.

---

## 6. Purchase & Reorder Planning (BRD C.4)

**Use case:** The purchase team reviews an **auto-generated** reorder plan (they don't type
suggested quantities) and raises real POs from it.

**Where:** `pacific-tileflow` → Purchase Requirements (`/requirements`), Purchase Orders
(`/purchase-orders`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-PUR-01 | Reorder suggestion appears for a genuinely short item | Have an item with zero/low stock and real open retail demand (missed-demand inquiries + pending inquiries). | Item surfaces as Critical/urgent on `/requirements` with a computed suggested quantity, stock, missed demand, pending inquiries, and reason breakdown all shown. | C.4.1 | M2 / Wave 1 |
| TC-PUR-02 | Bulk/project demand excluded | Log a large Bulk-dealer order for an item. | This demand does **not** inflate that item's reorder suggestion — retail/sub-dealer channel only, per C.4.3. | C.4.1 / C.4.3 | M2 / Wave 1 |
| TC-PUR-03 | Discontinued items excluded | An item with real missed demand but status Factory Discontinued or Pulled Back. | Excluded from reorder suggestions, with the reason stated as the actual status, not a generic "N/A". | C.4.1 / C.10.5 | M2 / Wave 1 |
| TC-PUR-04 | Raise PO from a suggestion | On `/requirements`, click "Raise PO" for a suggested item. | A real Purchase Order is created, supplier/quantity prefilled from the suggestion, remarks auto-composed from the reason breakdown. | C.4.1 | M2 / Wave 1 |
| TC-PUR-05 | PO line progress tracking | Open a PO's detail (`/purchase-orders/$id`). | Ordered/ready/planned/received quantities shown per line, ready-qty editable inline. | C.4.4 | M2 / Wave 1 |
| TC-PUR-06 | Plan Inward from a PO | On a PO line with quantity still to plan, click "Plan Inward". | A real Inward Truck is created, linked back to the PO/line, appears immediately in `/inward`. | C.4.4 | M3 / Wave 1 |
| TC-PUR-07 | Reorder plan doesn't go stale | Raise a PO against a suggestion, then revisit `/requirements`. | The plan reflects the new PO's effect (in-transit/pending quantity), not the pre-PO numbers. | C.4.1 | M2 / Wave 1 |

### Known gaps — not yet built

- **C.4.2** Full MOQ-hierarchy enforcement (company/vendor/item, production vs stock MOQ)
  and vendor-enquiry readiness capture beyond what the reorder suggestion already surfaces.
- **C.4.4** Purchase receipt shortage/claim capture and supplier invoice reference at
  receipt time — `/inward` doesn't yet record a short/mismatched receipt against
  expectation.

---

## 7. Supplier Pickup & Route Planning (BRD C.5)

**Use case:** Purchase plans a pickup run against one supplier's ready POs.

**Where:** `pacific-tileflow` → Pickup Planner (`/pickup-planner`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-PICK-01 | Create a pickup run | Pickup Planner → New → pick vehicle type, vehicle number, scheduled date, driver, supplier. | Run created in Draft status. | C.5 | M3 / Wave 1 |
| TC-PICK-02 | Add line items from ready POs | Add one or more line items, each a specific item from that supplier's ready Purchase Orders. | Lines attach correctly; more can be added later. | C.5 | M3 / Wave 1 |
| TC-PICK-03 | Advance run status | Move the run Draft → Dispatched → Completed. | Each transition is tracked and visible on the run detail. | C.5 | M3 / Wave 1 |
| TC-PICK-04 | Create/manage a vehicle type master | Pickup Planner → manage vehicle types (truck/container tonnage tiers per BRD C.1.7's fleet table). | Vehicle types are created/listed correctly and selectable when creating a run. | C.1.7 / C.5 | M3 / Wave 1 |

### Known gaps — not yet built

Everything BRD asks for **beyond a single-supplier run**: a multi-supplier route across
several stops in one truck run, container-vs-truck auto-selection, Leaflet map/GPS pins,
drag-drop stop resequencing, "Auto-Generate Route" by proximity, live vehicle tracking
(C.5.1), a driver master, and "Share with Driver"/printable route sheet. Per BRD F.2, this
advanced routing/tracking layer is explicitly **Wave 2** ("assisted route planning and
truck-capacity planning") — Milestone 4. Do not test these — they don't exist yet, and
they're not expected yet either.

---

## 8. Inventory: Batch, Bay, QR & Scanning (BRD C.6)

**Where:** `pacific-tileflow` → Bays (`/bays`), Allocations (`/allocations`), Buffer Bays
(`/buffer-bays`), Transfers (`/transfers`), Stock (`/stock`), Scan (`/scan`), Picking
(`/picking`), Unallocated Stock (`/unallocated-stock`).

### 8.1 Bay Allocation Workflow (C.6.3)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WH-01 | System-suggested bay allocation | Allocations → New → pick item/quantity. | System suggests a suitable bay (matching category, checking capacity), highlighted on the map. | C.6.3 | M3 / Wave 1 |
| TC-WH-02 | Overflow falls back to a buffer bay | Suggest an allocation larger than the main bay's remaining capacity. | System falls back to a linked buffer bay (or a main+buffer split). | C.6.3 | M3 / Wave 1 |
| TC-WH-03 | Confirm/modify/split before printing | On the suggestion step, manually override the suggested bay or split the quantity. | Inline validation catches an invalid override (e.g. exceeding capacity); confirming produces a printable slip. | C.6.3 | M3 / Wave 1 |
| TC-WH-04 | Slip carries a real QR code | Print an allocation slip. | QR code renders correctly and scans back to the correct allocation. | C.6.3 / C.6.4 | M3 / Wave 1 |
| TC-WH-05 | Occupancy recomputes after scan confirmation | Complete a putaway scan for a confirmed allocation. | Bay occupancy on the Visual Warehouse Map updates immediately. | C.6.3 / C.6.5 | M3 / Wave 1 |

### 8.2 Buffer Bay Management (C.6.3 continued)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WH-06 | Days-in-buffer aging | Have stock sitting in a Buffer bay for more than 7 days. | An alert banner appears; suggested main-bay destination is shown. | C.6.3 | M3 / Wave 1 |
| TC-WH-07 | One-click transfer to main | From the alert, transfer buffer stock to its suggested main bay. | Stock moves; buffer occupancy decreases, main bay occupancy increases. | C.6.3 | M3 / Wave 1 |
| TC-WH-08 | Buffer bay linked correctly to main | Create a buffer-to-main link (TC-MD-28) then trigger TC-WH-06/07. | The suggested destination matches the explicitly linked main bay, not an arbitrary one. | C.6.3 | M3 / Wave 1 |

### 8.3 Material Transfer (Transfers)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WH-09 | Transfer between bays | Transfers → New → source/destination bay, item/batch, quantity, reason. | Validated (source actually holds the item/batch, destination has capacity) and recorded in transfer history. | C.6.3 | M3 / Wave 1 |
| TC-WH-10 | Damage-type transfer captures claim fields | Transfer with reason = damage. | Damage-type/claim-reference fields appear and are required. | C.6.3 / C.8 | M3 / Wave 1 |

### 8.4 Visual Stock Balance (Stock)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WH-11 | Map/List/Card views agree | Toggle between the three views for the same warehouse. | Same underlying stock, three presentations. | C.6.2 | M3 / Wave 1 |
| TC-WH-12 | Stock-age filter | Filter for stock older than 30/60 days. | Only qualifying lots appear. | C.6.2 | M3 / Wave 1 |
| TC-WH-13 | Top-3 batches shown, drill-down available | Open an item with more than 3 batches. | Top-3 by quantity shown by default; full batch list available on request (C.6.2). | C.6.2 | M3 / Wave 1 |

### 8.5 QR/Barcode Scanning (Scan)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WH-14 | Inward placement scan — match | Scan mode: Inward Placement → scan item → scan the correct bay. | Confirms and updates stock/bay. | C.6.5 | M3 / Wave 1 |
| TC-WH-15 | Inward placement scan — mismatch | Same, but scan a different bay than assigned. | Alerts mismatch, blocks the action. | C.6.5 | M3 / Wave 1 |
| TC-WH-16 | Transfer scan | Scan mode: Transfer → source → item → destination → confirm. | Transfer recorded exactly as the manual Transfers flow would. | C.6.5 | M3 / Wave 1 |
| TC-WH-17 | Picking scan validates against the pick list | Scan mode: Picking, scan an item not on the active pick task. | Rejected — validated against the specific pick list, not accepted blindly. | C.6.5 | M3 / Wave 1 |
| TC-WH-18 | Bay audit scan | Scan mode: Bay Audit, count differs from expected. | Variance shown inline (no persistence of the audit session — by design, not a bug). | C.6.5 | M3 / Wave 1 |

### 8.6 Picking Module (C.6.5's pick-list execution)

**Use case:** A dedicated Pick Task per order line — finer-grained than ERPNext's native,
all-or-nothing Pick List — with a suggested bay, a partially-allocatable quantity, a named
picker, and a Pending/Allocated/Picked status.

**Where:** `pacific-tileflow` → Picking (`/picking`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WH-19 | Pick tasks list for an order | Confirm an order that needs picking → open Picking. | One Pick Task per order line appears, status Pending. | C.6.5 / C.3.1 | M3 / Wave 1 |
| TC-WH-20 | Auto-allocate suggests a bay | Trigger auto-allocate on a pending task. | A suggested bay is proposed based on live stock (via `warehouse/utils.py`), not a stale cached view. | C.6.5 | M3 / Wave 1 |
| TC-WH-21 | Partial allocation | Allocate less than the full requested quantity to a task. | Task reflects partial status correctly; remaining quantity still needs allocation. | C.6.5 | M3 / Wave 1 |
| TC-WH-22 | Assign a named picker and mark Picked | Assign a picker to a task, advance it to Picked (via `patch_task`, or via the Scan → Picking mode in TC-WH-17). | Status updates; **note** — marking a task Picked does *not* itself create a Delivery Note or reduce stock (a deliberate scope boundary, not a bug) — confirm this doesn't surprise QA expecting automatic stock reduction. | C.6.5 | M3 / Wave 1 |

### 8.7 Loose / Unallocated Stock (C.6.6)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WH-23 | Record ad-hoc loose stock | Scan an item at a location that isn't an allocated bay. | Recorded as unallocated stock (item, batch, quantity, location); appears on the unallocated-stock report. | C.6.6 | M3 / Wave 1 |
| TC-WH-24 | Allocate loose stock to a proper bay | From the unallocated-stock report, allocate an entry to a real bay. | Standard Bay Allocation Slip flow runs; entry clears from "unallocated". | C.6.6 | M3 / Wave 1 |
| TC-WH-25 | Consolidate loose stock | From the report, consolidate a loose entry into existing open/partial stock of the same item/batch. | Quantities merge; no duplicate/orphaned stock record remains. | C.6.6 | M3 / Wave 1 |

### 8.8 Sticker printing (C.6.4)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-WH-26 | Print box/inward stickers | From a confirmed allocation, print box stickers. | Sticker shows item code, name, size/finish/series, batch (incl. manufacturing date), bay, boxes/pieces, weight, PR/supplier reference. | C.6.4 | M3 / Wave 1 |
| TC-WH-27 | Dealer sample sticker | Issue a sample (see §11). | Sticker carries a dealer-specific unique code, human-readable product name, series/size/finish, and the sample's batch. | C.6.4 / C.10.1 | M3 / Wave 1 |

---

## 9. Pricing Engine (BRD C.7)

**Where:** `pacific-tileflow` → Pricing (`/pricing`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-PRC-01 | Series price setting / landing cost | Pricing → select a pending product → enter purchase/freight/handling/other costs and margin %. | Landing cost and suggested price compute live (`Landing Cost × (1 + margin%)`). | C.7.1 | M2 / Wave 1 |
| TC-PRC-02 | Approve & publish | Approve the computed price. | `dealerPrice` updates everywhere it's read (Stock, New Inquiry, Quotation Builder) immediately, plus a history entry is recorded. | C.7.1 | M2 / Wave 1 |
| TC-PRC-03 | Three price lists derive correctly | Check the published price against each dealer tier (Standard/Dealer/Master Dealer). | Each tier's rate is correctly derived — not all three showing the same flat number. | C.7.1 / C.1.4 | M2 / Wave 1 |
| TC-PRC-04 | Per-dealer tier price override | Set an explicit tier-price override for one dealer (`set_dealer_tier_price`), distinct from that dealer's default classification-based rate. | The override applies for that dealer specifically; other dealers on the same tier are unaffected. | C.7.1 / C.1.4 | M2 / Wave 1 |
| TC-PRC-05 | Quotation-level price rework doesn't touch the master list | Rework freight/margin/discount inside a single quotation. | The change is scoped to that quotation only; the published dealer price list is untouched. | C.7.2 | M2 / Wave 1 |
| TC-PRC-06 | Retail markup override | Apply a markup % on a retail quotation. | Applies only at the quotation level, never mutating the base dealer price. | C.7.2 | M2 / Wave 1 |
| TC-PRC-07 | Any discount at all is audit-locked | Apply any nonzero `discount_percentage` on a Quotation/Order line, even 1%. | No threshold — this is BRD C.11 trigger #4, fully wired (see §13 TC-APR-06): Management applies it immediately (audit-logged), any other role gets the request queued for approval instead of applied. | C.7.3 / C.11 | M2 / Wave 1 |

---

## 10. Damage & Insurance Claims (BRD C.8)

**Where:** `pacific-tileflow` → Damage (`/damage`), Claims (`/claims`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-DMG-01 | Identify damage | Damage → New → source bay, item/batch, quantity, damage type, photo, auto-suggested damage bay. | Damage record created; stock moves to the damage bay. | C.8 | M3 / Wave 1 |
| TC-DMG-02 | Damage → Insurance Claim transfer | From a damage record, transfer to Insurance Claim with insurer/amount. | Creates a real, structured `InsuranceClaim` (not free text) — Filed status. | C.8.1 | M3 / Wave 1 |
| TC-DMG-03 | Claim lifecycle | Advance a claim Filed → Approved → Settled (with its own settlement amount, which can differ from the claimed amount) or Rejected. | Each transition updates the claims ledger; receivable/settled totals recompute correctly. | C.8.1 | M3 / Wave 1 |
| TC-DMG-04 | Printable claim voucher | Open a filed/settled claim. | A printable voucher is available with insurer/amount/consignment references. | C.8.1 | M3 / Wave 1 |
| TC-DMG-05 | Damage screen links to Claims totals | Open `/damage`. | Banner shows live pending-receivable and settled totals, linking to `/claims`. | C.8.1 | M3 / Wave 1 |
| TC-DMG-06 | Claims accumulated by supplier | File several small claims against the same supplier. | `accumulated_claims_by_supplier` groups them correctly — small individual values rolling up into one supplier-wise accumulation before a voucher, per BRD's own framing (small values accumulated, always grouped by supplier/company). | C.8 | M3 / Wave 1 |
| TC-DMG-07 | Year-end reconciliation list | Check `claims_pending_year_end_reconciliation` around the 31 March boundary with an open claim still pending. | The claim surfaces as needing year-end journal treatment (removed 31 March, reinstated 1 April) — verify the report identifies it, even if the actual journal-posting automation is manual/assisted rather than fully automatic. | C.8.2 | M3 / Wave 1 |
| TC-DMG-08 | Claim settled via journal posting | Settle a claim and confirm the accounting journal entry (`finance/accounting.py`) posts against the configured bank account. | Journal entry created correctly, referencing `Pacific Accounting Settings`' configured accounts. | C.8.2 | M3 / Wave 1 |
| TC-DMG-09 | Insurance vs Transit claim-type selector | File a claim, choosing Insurance vs Transit claim type. | Correct badge/type shows on the claims list; downstream settlement logic treats each per BRD C.8.2 (transit damage warehouse→customer is Pacific's own responsibility, handled by credit note). | C.8.1 / C.8.2 | M3 / Wave 1 |
| TC-DMG-10 | Shortage claim | File a claim from a receipt shortage with responsibility recorded (factory/driver/absorbed). | Same claim mechanism as transit/warehouse damage. *(Currently limited by the §6 receipt-shortage-capture gap — verify current behavior before assuming this is fully wired end-to-end from `/inward`.)* | C.8.3 / C.4.4 | M3 / Wave 1 |

---

## 11. Unloading & Labour Payments (BRD C.9)

**Where:** `pacific-tileflow` → Unloading (`/unloading`), Labour (`/labour`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-LAB-01 | Record an unloading charge | From an Inward truck card (past Scheduled), "Record unloading charge" → contractor, boxes, rate/box, payment mode. | Charge amount computes (`boxes × rate`), status Pending. | C.9.1 | M3 / Wave 1 |
| TC-LAB-02 | Mark unloading charge paid | Mark a Pending charge Paid. | `/unloading` and the originating `/inward` truck card both update live, no reload needed. | C.9.1 | M3 / Wave 1 |
| TC-LAB-03 | Printable unloading voucher | Open a recorded charge. | Voucher available for print. | C.9.1 | M3 / Wave 1 |
| TC-LAB-04 | Labour attendance entry | Labour → add an attendance day for a labourer (days/shift, work done). | Entry recorded; running dues update. | C.9.2 | M3 / Wave 1 |
| TC-LAB-05 | Record labour payment | Record a payment against a labourer's dues. | Payment mode/reference captured; outstanding dues reduce accordingly. | C.9.2 | M3 / Wave 1 |

---

## 12. Sample & Display Management (BRD C.10)

*Not explicitly named under any milestone in BRD F.1/F.2's own text — inferred here as
Milestone 3 (it's warehouse-adjacent lifecycle automation, tightly coupled to bay/QR work
that IS named there) and Wave 1 (BRD A.2 lists "automate sample and display lifecycle" as
a Phase 1 business objective, not a Wave-2 item).*

**Where:** `pacific-tileflow` → Samples Display (`/samples-display`); sample requests may
also be reachable from the Products/item detail panel.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-SMP-01 | Request a sample | Create a Sample Request for a dealer + item. | Sits in a pending-approval list. | C.10.1 | M3 / Wave 1 |
| TC-SMP-02 | Approve a sample request | Approve the pending request. | Moves to approved, ready to issue. | C.10.1 | M3 / Wave 1 |
| TC-SMP-03 | Issue a sample | Issue the approved sample. | Real stock movement/invoice occurs; item leaves the pending list; a dealer-specific sample sticker (unique QR) is generated. | C.10.1 | M3 / Wave 1 |
| TC-SMP-04 | Auto-created Display Placement Slip | Immediately after issuing (TC-SMP-03). | A Display Placement Slip is auto-created (item, dealer/location, quantity, placement date, photo); its placement date starts the monitoring clock. | C.10.2 | M3 / Wave 1 |
| TC-SMP-05 | Monitoring reminder job | Placement date crosses a configured interval (3/6/12 months) — check via the scheduled job `send_display_monitoring_reminders`. | A WhatsApp/email reminder with response buttons is sent for that placement. | C.10.2 | M3 / Wave 1 |
| TC-SMP-06 | Reconciliation records the outcome | Respond to a monitoring reminder / manually record a reconciliation ([Still displayed]/[Removed]/[Loose, not shown]). | A Sample Display Reconciliation record is created/updated with that outcome. | C.10.3 | M3 / Wave 1 |
| TC-SMP-07 | Pullback on discontinuation | Pull back a display for a discontinued/removed item. | Stock returns to the warehouse with returned-condition tracked; feeds the resell/write-off/clearance decision. | C.10.4 | M3 / Wave 1 |
| TC-SMP-08 | Top Dealer identification | Check top-dealer logic against 6 months of retail sales, bulk/project excluded. | Correct dealers surface as "top" for sample/display planning. | C.10.4 | M3 / Wave 1 |
| TC-SMP-09 | Product withdrawal automation reaches display removal | Let an item's display duration/store-count/annual-sales thresholds breach per its Series' configured criteria. | The daily `evaluate_product_withdrawals` job advances its lifecycle status and auto-triggers the removal + put-up list and pullback, without manual action. | C.10.5 | M3 / Wave 1 |
| TC-SMP-10 | Print sticker for an already-issued sample | Reprint a sticker for a sample issued earlier (not just at issue time). | Sticker regenerates identically (same QR/code), doesn't create a duplicate/conflicting record. | C.6.4 / C.10.1 | M3 / Wave 1 |

---

## 13. Approvals & Workflow (BRD C.11)

*Not explicitly named under any milestone in BRD F.1/F.2 — inferred here as Milestone 2-3
(a cross-cutting control layer over masters/pricing/orders that are themselves M2/M3),
Wave 1.*

Per BRD C.11, six explicit override triggers are audit-locked (need Management approval,
not just a submission): (1) credit-limit exceedance, (2) overdue outstanding, (3) any
pricing override — even ₹1, (4) any discount over the default price list, (5)
amendment/cancellation of a submitted sales/purchase document, (6) retail-vs-bulk
classification override. Rather than a bespoke approval flow per trigger or Frappe's native
Workflow doctype, this is one generic queue/ledger (`Approval Request`) plus three
endpoints (`list_pending_approvals`/`get_approval`/`decide_approval`) and a small
per-trigger "applier" — the same screen and decision flow works for every trigger as it
gets wired up.

**Four of the six are wired and testable: #6, #5, #4, #1.** In every case, an
already-authorized caller (Management/System Manager) still gets their action applied
immediately — just now with an audit-trail `Approval Request` logged as auto-approved —
while anyone else's attempt is queued as **Pending** instead of applied; the API returns
`{"approvalRequired": true, "approval": {...}}` in place of the normal result, and nothing
changes until Management decides it.

**Where:** `pacific-tileflow` → Approvals (`/approvals`) for listing/deciding; the triggers
themselves fire from `dms_erp.sales.quotation_api`/`order_api`'s own write endpoints
(create/edit/cancel), not from a dedicated screen.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-APR-01 | List pending approvals (Management) | Sign in as Management/System Manager, open `/approvals`. | Lists Approval Requests, default-filtered to Pending; filterable by status and by trigger type (all six named, even the two not yet raised). | C.11 | M2-3 / Wave 1 |
| TC-APR-02 | Non-Management cannot list or decide | Sign in as Sales/Warehouse/Purchase, call `list_pending_approvals`/`decide_approval` (or open `/approvals`). | A permission error, not an empty list — "Only Management can decide approval requests." | C.11 | M2-3 / Wave 1 |
| TC-APR-03 | Channel Override (#6) by Management — applies immediately | As Management, create a Quotation/Order passing an explicit `channel` that differs from what the dealer/lines would auto-classify to. | The document is created with the requested channel right away; an Approval Request is logged with status **Approved**, `decidedBy` = the acting user, referencing the new document. | C.4.3 / C.11 | M2-3 / Wave 1 |
| TC-APR-04 | Channel Override (#6) by Sales — queued | As Sales, do the same explicit-channel override. | No document is created. Response is `{"approvalRequired": true, "approval": {...status: "Pending"}}`; the request shows up in `/approvals`. | C.4.3 / C.11 | M2-3 / Wave 1 |
| TC-APR-05 | Channel matching auto-classification never gates | As Sales, create a Quotation/Order with `channel` left unset (or matching what auto-classification would pick anyway). | Created normally, no Approval Request raised at all — not even an Approved one. | C.4.3 / C.11 | M2-3 / Wave 1 |
| TC-APR-06 | Discount Over Price List (#4) — any discount at all | As Sales, create a Quotation or Order with any line's `discount_percentage` above 0 (even 1%). | Same queued/immediate split as TC-APR-03/04, trigger type "Discount Over Price List" — no threshold; a 1% discount gates exactly like a 50% one. | C.7.3 / C.11 | M2-3 / Wave 1 |
| TC-APR-07 | Combined override in one call | As Management, create a Quotation with both an explicit channel override AND a discounted line in the same call. | Both apply immediately; **two** separate Approval Request rows are logged (one per trigger type), both referencing the same created Quotation. | C.7.3 / C.4.3 / C.11 | M2-3 / Wave 1 |
| TC-APR-08 | Amend/Cancel (#5) — editing a submitted quotation | As Sales, add/remove a line or change a line's qty on an already-submitted Quotation (`add_quotation_line`/`remove_quotation_line`/`update_quotation_line_qty`). | Queued, not applied — the quotation is untouched (still its original line values) until Management decides it. As Management, the same edit applies immediately (new amended document, audit-logged). | C.11 | M2-3 / Wave 1 |
| TC-APR-09 | Amend/Cancel (#5) — cancelling a submitted order | As Sales, move a Sales Order's fulfillment stage to "Cancelled" (`advance_order_stage`). | Queued, not applied — order stays at its current stage. As Management, cancels immediately, audit-logged. Every other forward-flow transition (Confirmed→Picking→…) is unaffected — only "Cancelled" gates. | C.11 | M2-3 / Wave 1 |
| TC-APR-10 | Credit Limit Exceeded (#1) — order creation | As Sales, create/convert an order for a dealer whose `Customer Credit Limit` (configured per company) would be exceeded by this order's own value plus their already-committed Sales Order value. | Queued for both `create_order` (Inquiry-sourced) and `convert_to_order` (Quotation-sourced); Management's own attempt applies immediately, audit-logged. | C.11 | M2-3 / Wave 1 |
| TC-APR-11 | Credit Limit Exceeded (#1) — no limit configured | Create a large order for a dealer with no `Customer Credit Limit` row at all. | Never gates, regardless of order size — an untagged dealer behaves exactly as before this feature existed. | C.11 | M2-3 / Wave 1 |
| TC-APR-12 | Approve a queued request | As Management, open a Pending request in `/approvals` and Approve it (optionally with a note). | The original action replays and actually happens now (document created/edited/cancelled); the request's status becomes Approved, `referenceName` is stamped to whatever was actually created/changed. | C.11 | M2-3 / Wave 1 |
| TC-APR-13 | Reject a queued request | As Management, Reject a Pending request instead. | Nothing is created/changed; status becomes Rejected. The requester must resubmit without the override (or get it resolved directly). | C.11 | M2-3 / Wave 1 |
| TC-APR-14 | Deciding an already-decided request | Try to Approve/Reject a request that's already Approved or Rejected. | Rejected with a validation error — a decision is final, not re-appliable. | C.11 | M2-3 / Wave 1 |

### Known gaps — not yet built

- **#2 Overdue Outstanding** — not wired, and not buildable with a gate alone. "Overdue"
  means an invoice past its due date and still unpaid; this app posts no Sales Invoice or
  Payment Entry at all (dashboards' own `outstandingReceivables` is a documented, honest
  `0`). A real #2 needs an AR/invoicing subsystem built first — don't file a bug for its
  absence, there's nothing today to build a check against.
- **#3 Pricing Override** — not wired as a separate trigger. BRD C.7.3 talks about "any
  transaction-level price or discount change" as one idea, but this codebase only has one
  transaction-level pricing lever (`discount_percentage`), already fully covered by #4
  (TC-APR-06/07 above exercise it). There's no explicit-rate-override field a caller can
  set instead of a discount — a real, distinct #3 needs that capability built first.
- No mobile-specific approval flow, no WhatsApp/email/SMS notification-on-raise (the BRD's
  `channels_notified` concept) — deciding still requires opening `/approvals` in the staff
  web app.

---

## 14. Reporting, Dashboards & Assisted AI (BRD C.12)

### 14.1 Dashboards (C.12.1)

**Where:** `pacific-tileflow` → Dashboard (`/dashboard`), which branches by signed-in role.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-RPT-01 | Role-appropriate dashboard | Sign in as each role (Sales/Warehouse/Purchase/Management). | Each sees the KPIs/alerts relevant to their function (inquiry activity; bay occupancy/pending allocations/damage; reorder/PO status; cross-functional summary). | C.12.1 | M4 / Wave 1 |
| TC-RPT-02 | Alert cards are live and clickable | Click a dashboard alert card (e.g. a pending allocation, damage awaiting claim, item below safety stock). | Navigates straight to the screen where you'd act on it. | C.12.1 | M4 / Wave 1 |
| TC-RPT-03 | Numbers aren't stale/cached | Compare a dashboard count against the underlying screen's live detail. | They agree — the dashboard recomputes each load, it doesn't cache. | C.12.1 | M4 / Wave 1 |

### 14.2 Report Set (C.12.2)

**Where:** `pacific-tileflow` → Reports (`/reports`), one flat searchable list across 6
categories: Sales, Warehouse, Purchase, Finance, Catalog, Forecasting.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-RPT-04 | Every report loads with real data | Open each of the ~20 reports in turn (Inquiry register, Duplicate inquiry, Missed demand, Inquiry-to-PO mapping, Reorder planning, Purchase pickup plan, Bay occupancy, Visual stock balance, Consolidated/fragmented stock, Stock movement/ageing, Short stock, Retail vs bulk, Damage & claims, Unloading payment, Pricing & CSP, Fast/slow-moving product, Stock clearance, Display placement/ageing, Dealer/salesperson performance, Demand forecast). | Each loads without error and reflects real backend data — this module has **no mock fallback**, so an unconfigured backend shows an honest "no backend connected" state rather than fabricated numbers. | C.12.2 | M4 / Wave 1 |
| TC-RPT-05 | Filters narrow results correctly | Apply a filter (date range, dealer, item, category — whichever the report offers) on any report. | Result set narrows correctly; clearing the filter restores the full set. | C.12.2 | M4 / Wave 1 |
| TC-RPT-06 | CSV export | Export any report to CSV. | File downloads and matches what's shown on screen. | C.12.2 | M4 / Wave 1 |
| TC-RPT-07 | Click-to-sort | Click a sortable column header. | Table re-sorts client-side correctly. | C.12.2 | M4 / Wave 1 |
| TC-RPT-08 | Demand forecast chart | Open the Forecasting report. | Renders as a real chart (the one report with a chart; all others are tables). | C.12.2 | M4 / Wave 1 |

### 14.3 Salesperson Assignment (C.12.3)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-RPT-09 | Assign a salesperson to a dealer | Dealers → edit → set salesperson. | Assignment saved; appears in the Salesperson Assignment report. | C.12.3 | M4 / Wave 1 |

### Known gaps — not yet built

- **C.12.4** Customer PO OCR & product matching — no OCR/AI ingestion of customer POs.
  Feasibility itself is still open per the BRD's own wording ("subject to feasibility").
- **C.12.5** Assisted AI Wave 2 — image search, AI recommendation, AI forecasting —
  explicitly Wave 2 per the BRD's own section header; do not test.
- **C.12.6** Future Customer API Integration — the BRD's own "interim approach" (dealer
  enters their own PO number to tag and close an order) **is** built (see
  TC-SO-13/TC-WA-16); a real dealer-ERP-to-Pacific API integration is not (and is
  explicitly "feasibility to be evaluated" per BRD, not scheduled to any wave yet).

---

## 15. Web Dealer App — Enquiry Module (BRD C.13)

**Where:** `dms-dealer-portal` (separate app from the staff `pacific-tileflow`).

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-DP-01 | Phone + OTP login, no password | Open the dealer portal → enter the test dealer's phone number → enter the OTP received. | Logs in; session is scoped server-side to that dealer only. | C.13.1 | M3 / Wave 1 |
| TC-DP-02 | Wrong/expired OTP rejected | Enter an incorrect OTP, then a correct one after the code has expired. | Both rejected cleanly; a fresh OTP request succeeds. | C.13.1 | M3 / Wave 1 |
| TC-DP-03 | Catalog scoped to this dealer | Browse the catalog. | Only items in this dealer's Dealer Catalog assignment appear, each with images (product/application/additional) and price where enabled. | C.13.1 | M3 / Wave 1 |
| TC-DP-04 | Search by either code | Search using the dealer's own private code, then the company code, for the same item. | Both resolve to the same item detail page. | C.13.1 / C.1.5 | M3 / Wave 1 |
| TC-DP-05 | Item detail shows stock/batches/price | Open an item's detail page. | Real-time stock, top-3 batches, and price (if enabled for this dealer) are shown. | C.13.1 / C.6.2 | M3 / Wave 1 |
| TC-DP-06 | Enquiry → order (in stock) | Raise an enquiry on an in-stock item → convert to order, entering the dealer's own PO number. | Real Sales Order created, tagged with that PO number. | C.13.1 / C.12.6 | M3 / Wave 1 |
| TC-DP-07 | Enquiry stays an enquiry (out of stock) | Raise an enquiry on an out-of-stock item. | Recorded as an inquiry/out-of-stock request; suggested alternatives shown, not silently dropped. | C.13.1 | M3 / Wave 1 |
| TC-DP-08 | Order/enquiry/dues history | Open Orders, Inquiries, Profile tabs. | Correct history and outstanding dues for this dealer only. | C.13.1 | M3 / Wave 1 |
| TC-DP-09 | Cross-dealer isolation | Attempt to access another dealer's order/inquiry by guessing an ID/URL. | Rejected — this dealer's session cannot read another dealer's data (mirrors the WhatsApp cross-dealer checks, TC-WA-19). | B.2 / C.13 | M3 / Wave 1 |

---

## 16. Catalog & Price-List Formats for Dealers (BRD C.14)

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-CAT-01 | Dealer catalog export | Export a dealer-scoped catalog (`dealer_catalog_export`). | Contains only that dealer's visible items, with images and key attributes. | C.14.1 | M2 / Wave 1 |
| TC-CAT-02 | Price-inclusive vs price-exclusive | Export both variants for a dealer with pricing enabled. | Price-inclusive shows their tier's price/MRP; price-exclusive omits price entirely. | C.14.2 | M2 / Wave 1 |
| TC-CAT-03 | Dealer sees only their applicable price list | Compare exports for a Standard Dealer vs a Master Dealer on the same item. | Each shows their own tier's rate — never another tier's. | C.14.2 / C.1.4 | M2 / Wave 1 |

---

## 17. End-to-End Integration Scenarios (cross-cutting)

Not separate BRD sub-sections — full lifecycle traces through several modules at once,
matching the BRD's own emphasis on traceability from inquiry to closure (C.2.3) and
order-to-cash (Figure 4). Run these after the module-level test cases above pass
individually; they catch integration bugs that per-module testing alone won't.

| ID | Scenario | Steps | Expected Result | BRD Ref | Milestone / Wave |
|---|---|---|---|---|---|
| TC-E2E-01 | Full order-to-cash, staff-initiated | Log an Inquiry → build a Quotation from it → convert to Order → advance through Picking → Bay-driven dispatch → confirm stage reaches Delivered. | Every stage's reference chain (Inquiry → Quotation → Order → Pick Task → dispatch) is traceable end to end; no step silently loses the link to the one before it. | C.2.3 / C.3.1 / C.6.5 | M2-M3 / Wave 1 |
| TC-E2E-02 | Full order-to-cash, dealer-initiated via WhatsApp | Dealer checks stock via WhatsApp → Place Order with PO number → staff advances the resulting Sales Order through to Delivered → dealer checks Order Status via WhatsApp at each stage. | The WhatsApp-created order behaves identically to a staff-created one throughout its whole lifecycle — no special-cased or second-class order type. | C.2.2 / C.3.1 / C.12.6 | M2-M3 / Wave 1 |
| TC-E2E-03 | Short stock → reorder → PO → inward → bay → dealer notified | Item goes short (via inquiries) → surfaces on `/requirements` → Raise PO → Plan Inward → putaway via Bay Allocation → dealer re-checks the same item via WhatsApp. | The dealer's WhatsApp reply reflects the newly-arrived stock immediately once putaway is confirmed — no caching lag between warehouse and dealer-facing reads. | C.4.1 / C.6.3 / C.2.2 | M2-M3 / Wave 1 |
| TC-E2E-04 | Damage discovered mid-fulfillment | An order is Picking → item found damaged during picking/scan → damage recorded, moved to Damage bay → claim filed → order re-picked from remaining/replacement stock. | The order's own progress isn't silently corrupted by the damage side-track; the claim and the order both end up in correct, consistent states. | C.6.5 / C.8 / C.3.1 | M3 / Wave 1 |
| TC-E2E-05 | Sample issued → converts into a real order | Issue a sample to a dealer → dealer later inquires/orders the same item for real (not the sample) via WhatsApp or the dealer portal. | Sample issuance and the later real order are correctly distinct records — the sample's batch doesn't force the same batch onto the real sale (BRD C.10.1's own explicit rule). | C.10.1 / C.2.2 / C.13.1 | M3 / Wave 1 |

---

## Appendix: Known gaps — not yet built (system-wide summary)

For quick reference — do not file these as bugs, and don't spend QA time hunting for them:

- **C.2.6** Proactive WhatsApp follow-up reminders (dealer or staff CRM) — genuinely new
  capability, deliberately blocked on confirming whats91's real template/button-reply API.
  Milestone 3, Wave 1 (not built, not a Wave-2 deferral).
- **C.3.2** Supplier-vs-customer timeline/delay-impact comparison and the
  stock-in-hand/in-transit/factory-dispatch forecasting window.
- **C.3.3** Bill-to/Ship-to validation, drop-ship, and automatic e-Way Bill generation —
  confirmed absent from the codebase entirely, not merely untested.
- **C.3.4** The real India Banking / VALS API integration (only an interim manual gate
  exists).
- **C.4.2/C.4.4** Full MOQ-hierarchy enforcement; purchase-receipt shortage/claim capture
  and supplier invoice reference at receipt time.
- **C.5/C.5.1** Multi-supplier route planning, map/GPS, drag-drop stops, live vehicle
  tracking, driver master, "Share with driver." Milestone 4, Wave 2 — scheduled later, not
  simply missing.
- **C.11** Four of six override triggers are built (§13): credit-limit exceedance,
  discount-over-price-list, amend/cancel, and channel override. Still missing: overdue
  outstanding (needs an AR/invoicing subsystem this app doesn't have at all — not a gap to
  chase, there's nothing to check), a separate "pricing override" trigger distinct from
  discount (collapses into the same field in this data model), mobile-specific approvals,
  and WhatsApp/email/SMS notification-on-raise.
- **C.12.4** Customer PO OCR — feasibility itself still open per the BRD.
- **C.12.5** Image-based AI search/recommendation/forecasting — explicitly Wave 2,
  Milestone 4, deferred by the BRD itself, not a gap to chase now.
- **Settings/Masters (BRD §13 Settings)** — no `/dealers`-adjacent Masters/Settings screens
  beyond what's covered above (e.g. no standalone Users/Masters admin screens for
  everything the nav implies).

If you find behavior that contradicts a "built" section above, that's a real regression —
file it. If you find a gap listed here behaving differently than "absent," that's worth a
note too, since it may mean partial work landed since this guide was last updated.
