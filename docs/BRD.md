# Pacific Inc — BRD v2.2 (Technical Solution & Development Guide)

> Source of truth is the original PDF: `BRD_v2.2.pdf` (same folder). This file is a verbatim
> text extraction (`pdftotext -layout`), kept plain rather than reformatted into markdown tables,
> so its content can't drift from the PDF through a lossy re-transcription. Numbers, thresholds
> and field specs below are exact quotes from the source document.
>
> **Not included here**: the flowchart/diagram figures (Figure 1-18) — those are visual aids
> whose logic is also described in the surrounding prose; open the PDF directly for the diagram
> itself if the text isn't enough.
>
> Structure: Part A (Business Context) · B (Solution Architecture) · C (Functional Requirements
> by Module, C.1-C.14) · D (Data Structures / Custom Doctypes) · E (Integrations) ·
> F (Implementation Approach) · G (Open Points & Configurable Values).

---


                                               PACIFIC INC
                                                  B2B TILES DISTRIBUTION

          Business Automation & Dealer Distribution System
                          Business Requirement & Technical Solution Document
                          Detailed Development & Implementation Guide (Frappe / ERPNext)

 Client                                              Pacific Inc
 Business Owner                                      Mr. Prashanth Bhupendra
 Prepared by                                         MicroNXT Solutions Pvt. Ltd.
 Platform                                            ERPNext (open-source)
 Document Type                                       Business Requirement Document (BRD) — Technical
 Version                                             2.2
                                                     BRD v2.1 + client detailing inputs (dealer bands, vehicle tracking, batch
 Basis
                                                     suggestion, labour, client dependencies)
 Status                                              For Development & Validation

                             Confidential — prepared for internal development, review and validation

Document Control
This is the technical Business Requirement Document (BRD) for Pacific Inc's Business Automation &
Dealer Distribution System, to be implemented by MicroNXT on ERPNext. It is written as a development
and implementation guide: it describes the solution architecture, the functional modules in operational
detail, the data structures of the custom (non-ERPNext) document types, the customisations required
over ERPNext defaults, the integrations, the automation logic, and the phased delivery plan.

  How to read this document
  Blue-bordered boxes are business rules or formulas. Purple-bordered boxes marked “Development note” flag
  where custom ERPNext development is required (a default ERPNext feature does not cover the need).
  Commercial figures are excluded; milestone scopes, the two-wave approach and the indicative timeline are
  included.
  Values left open in discussions (thresholds, bands, MOQ, reminder intervals) are configurable and listed in Part
  G.

Version History
 Version                                       Prepared By                     Description
 1.0                                           MicroNXT                        Initial requirement document.
                                                                               Technical BRD — architecture,
 2.0                                           MicroNXT                        module map, custom data structures,
                                                                               updated workflows.
                                                                               Detailing revision. Adds WhatsApp
                                                                               inquiry flows, detailed
                                                                               transporter/vehicle master,
                                                                               dealer-code identification,
                                                                               barcode/sticker formats, warehouse
                                                                               mobile scanning, dealer-app enquiry,
                                                                               catalog & price-list formats, image
                                                                               bulk-import, detailed bay-allocation,
 2.1                                           MicroNXT                        purchase-planning /
                                                                               sample-placement /
                                                                               product-withdrawal automation,
                                                                               migration detail, ERPNext
                                                                               customisation plan and
                                                                               implementation steps. Removes CSP;
                                                                               payment terms use ERPNext default;
                                                                               assisted-calling replaced by
                                                                               WhatsApp reminders.
                                                                               Second detailing revision. Confirmed
                                                                               dealer-classification thresholds;
                                                                               inquiry pricing by dealer type +
 2.2                                           MicroNXT                        purchase history; WhatsApp
                                                                               batch-combination suggestion; live
                                                                               vehicle tracking with stop/dwell
                                                                               capture and expanded transport

 Version                                       Prepared By                               Description
                                                                                         master; loose/unallocated stock
                                                                                         tracking; reorder review notification;
                                                                                         labour attendance & payment
                                                                                         doctype; consolidated
                                                                                         client-dependency list.

Change Log — What Changed in v2.1
 Area                                           Change in v2.1
                                                Detailed with the actual Morbi→Bengaluru full-load fleet (7 truck tonnages,
 Transporter & Vehicle master
                                                4 container tonnages by rail/ship).
                                                Sample conversation flows added, aligned to current dealer style; code
 WhatsApp inquiry                               identification by dealer code and company code; reminders via WhatsApp
                                                with response buttons.
                                                Removed. Follow-up is WhatsApp reminders with response buttons only
 Assisted calling
                                                (dealer follow-up and staff CRM follow-up).
                                                Dealer sees only items for which a sample has been issued; can search by
 Dealer codes & visibility
                                                own code or product code; dealer↔company code mapping detailed.
                                                Screen is a review-only window; quantities are auto-generated from historical
 Reorder planning
                                                sales.
 Series                                         Now carries bulk-quantity and retail-quantity thresholds.
 Supplier master                                GPS coordinates captured on supplier.
 Weight / UOM                                   Weight UOM conversion made explicit; weight shown on all documents.
                                                Barcode/QR printing and sticker formats (box sticker, dealer sample sticker)
 Barcode / stickers
                                                detailed.
 Warehouse scanning                             Mobile web scanning interface for inward, pick list, validation and dispatch.
 CSP                                            Removed entirely.
 Sample display reconciliation                  Added as a dedicated doctype.
                                                Uses the ERPNext default Payment Terms doctype (custom policy doctype
 Payment policy
                                                removed).
                                                Dealer-wise catalog with product/application/multiple images; price-list
 Catalog & price-list formats
                                                formats shared with dealer.
 Data migration                                 Expanded: current + last year, key steps, and customized Tally fields.
 Image import                                   Bulk import from Google Drive with filename-to-item-code matching.
 Dealer app                                     Enquiry module detailed.
                                                Detailed automation for purchase planning, sample placement and product
 Automation
                                                withdrawal; detailed bay-allocation logic.
 Implementation                                 Added ERPNext customisation plan and step-by-step implementation.

Change Log — What Changed in v2.2

 Area                                           Change in v2.2
                                                Price quoted during inquiry is derived from dealer type and the dealer's
 Inquiry pricing
                                                purchase history to date.
                                                Confirmed sales-based bands: below ₹1 lakh → Standard Dealer; ₹1 lakh and
 Dealer classification thresholds
                                                above → Dealer; ₹15 lakh and above → Master Dealer (configurable).
                                                Inquiry replies suggest the batch that meets the required quantity, or a 2–3
 WhatsApp batch suggestion
                                                batch combination when no single batch suffices.
                                                Live truck location, pickup capture per stop and stop/dwell-time capture;
 Vehicle tracking                               transport master expanded with owner and vehicle number; route/stops
                                                shared with driver by link or app.
                                                A flow to scan and track stock kept in empty, non-allocated space and route it
 Loose / unallocated stock
                                                to a proper bay.
                                                Responsible members are notified to review the auto-generated draft
 Reorder review notification
                                                reorder plan.
 Labour attendance & pay                        New doctype to track labourer attendance and payment.

 Sales Person Tagging                           Tag Sales Person to dealers that are assigned to them
                                                Accurate bay size/type list and warehouse layout/capacity to be provided by
 Bay sizes & warehouse design
                                                Pacific Team.
                                                Consolidated list of information required from the client (GST/tax structure,
 Client dependencies                            current series numbering & print formats, user list/roles/access, warehouse
                                                design).

Table of Contents

## Part A — Business Context & Objectives

### A.1 Background
Pacific Inc is a B2B tiles distributor selling primarily to dealers from a single Bangalore warehouse.
Current operations depend on Tally, phone calls, WhatsApp, manual follow-ups, vendor coordination and
Excel-based inquiry tracking, with heavy reliance on individual staff for stock visibility, purchase
follow-up, warehouse placement, sample movement, damage handling and reporting.

The objective is a structured, workflow-driven operating system — not merely a replacement accounting
package. A recurring principle is that the system must be action-oriented: it should identify exceptions,
maintain traceability and prompt users to act, rather than only recording transactions and producing
passive reports.

### A.2 Business Objectives
   •​ Capture every dealer inquiry in a structured way and drive it to a defined closure (order, PO linkage
      or reasoned drop).
   •​ Provide real-time, batch-aware, weight-aware stock visibility across the internal system, the web
      dealer app and WhatsApp.
   •​ Standardise product master creation through a Series-driven model that pre-sets attributes,
      pricing and bulk/retail thresholds.
   •​ Auto-generate purchase reorder plans from historical retail sales, presenting a review-only screen
      to the purchase team.
   •​ Plan supplier pickups and optimize vehicle (container vs truck) and route selection for the
      Morbi→Bengaluru corridor.
   •​ Manage warehouse bay allocation, QR/barcode-based movement and batch traceability, with a
      mobile scanning interface.
   •​ Enforce pricing, credit and discount controls through submit-freeze approvals with mobile approval
      and notifications.
   •​ Track damage, insurance claims, settlement and write-off with correct accounting linkage.
   •​ Automate unloading/labour payment tracking and vouchers.
   •​ Automate sample and display lifecycle — placement, monitoring, reconciliation and product
      withdrawal.
   •​ Deliver a web dealer app and WhatsApp channel with dealer-wise catalog (sample-issued items
      only) and images.
   •​ Deliver action-oriented dashboards and, in Wave 2, assisted AI (image search, recommendation,
      forecasting).

### A.3 Users & Roles
Approximately ten internal users are considered for initial sizing (no hard cap); around 150 dealers
interact through the app and WhatsApp. Menus and permissions are role-based.

 Role                                           Primary Functions
 Sales / CRM                                    Inquiry capture, quotation, dealer follow-up (WhatsApp), order confirmation.
                                                Purchase receipt, bay allocation, QR/scan, picking, dispatch, stock movement,
 Warehouse
                                                sample placement.
                                                Reorder review, vendor enquiry, purchase orders, pickup/route planning,
 Purchase
                                                supplier follow-up.
                                                Outstanding, advance/payment control, unloading payments, claims and
 Finance
                                                write-offs, e-Way bills.
                                                Approvals, overrides, dashboards. Owner (Mr. Prashanth) is notified of critical
 Management (Owner / Delegate)
                                                actions; a delegate (e.g. Mr. Dhananjay) holds override authority under audit.

## Part B — Solution Architecture & Module Map
The solution is layered into a channel layer (how dealers and staff interact), a middleware / API gateway
(which protects the core and enforces dealer-wise visibility), the core Frappe/ERPNext backend with
custom modules, and an integration layer. This map is the reference for Part C (modules), Part D (data
structures) and Part F.4 (customisation plan).

                                         Figure 1 — Solution architecture and module map

### B.1 Channel Layer
 Channel                                        Description
                                                Dealer-facing conversational channel using controlled menus and structured
                                                input (item-code / dealer-code search, image confirmation) aligned to how
 WhatsApp Interface                             dealers already message today. Supports stock inquiry,
                                                order/delivery/payment status, out-of-stock capture and follow-up reminders
                                                with response buttons. Phase 1.
                                                Browser-based self-service portal (desktop and mobile) for catalog
 Web-Based Dealer App                           (sample-issued items only), real-time stock, inquiries and orders, status, dues
                                                and history. Phase 1. Native iOS/Android and offline apps excluded.
 Internal ERP UI                                Desktop and mobile browser access for staff.
                                                A mobile web interface (and PDA) for the warehouse team to scan and
 Warehouse Mobile Scanning
                                                validate at inward, bay placement, picking (pick list) and dispatch.

### B.2 Middleware / API Gateway
Dealer-facing traffic (WhatsApp and the dealer app) passes through a middleware layer rather than
exposing the core ERP directly. The middleware restricts access, protects the core, enforces dealer-wise
catalog / pricing / eligibility consistently across both dealer channels, improves performance and exposes
only the required APIs.

  Development note — customisation required
  The middleware layer, the WhatsApp conversational flow engine and the dealer app portal are custom builds
  on top of ERPNext's REST/portal framework — they are not ERPNext default features.

### B.3 Core Backend Modules
 Module                                         Responsibility
                                                Series, Item, Dealer/Customer, Supplier (with GPS), Transporter & Vehicle,
 Master Data
                                                Warehouse & Bay, price lists, UOM/weight.
                                                Structured inquiry, code identification, duplicate detection, missed demand,
 Dealer Inquiry & CRM
                                                WhatsApp follow-up, closure via PO linkage.
                                                Quotation with price working, Sales Order from customer PO, reservation,
 Sales & Order Management
                                                delivery dates, drop-ship, delivery note, invoice, e-Way bill.
                                                Auto-generated reorder plan (review-only), MOQ handling, vendor enquiry,
 Purchase & Reorder Planning
                                                POs, receipt and shortage handling.
                                                Supplier GPS, pickup plans, container/truck capacity, route optimisation,
 Supplier Pickup & Route Planning
                                                driver route sharing.
                                                Batch and variable weight, bay master and allocation logic, QR/barcode
 Inventory: Batch, Bay, QR / Scan
                                                generation and sticker printing, mobile scanning validation, stock movement.
                                                Series price setting, three price lists, retail markup, quotation price working,
 Pricing Engine
                                                price-list formats for dealers.
                                                Damage identification, claim accumulation and voucher, settlement modes,
 Damage & Insurance Claims
                                                write-off and accounting linkage.
 Unloading Payments                             LR-wise unloading/labour charges, payment modes and vouchers.
                                                Sample approval, dealer-specific QR, placement automation, reconciliation,
 Sample & Display Management
                                                product withdrawal, pullback.
                                                Submit-freeze, override triggers, amend/cancel with version diff, mobile
 Approvals & Workflow
                                                approvals.
                                                Action-oriented dashboards and reports; Wave 2 assisted AI (image search,
 Reporting, Dashboards & AI
                                                recommendation, forecasting).

### B.4 Integration Layer
 Integration                                    Purpose
 WhatsApp / Meta                                Dealer messaging; subject to Meta approvals and template approval.
                                                Real-time reflection of bank transactions for advance-payment and dispatch
 India Banking (VALS API)
                                                control; integrated with ERPNext.

 Integration                                    Purpose
 GST & e-Way Bill API                           GST invoicing and automatic e-Way bill generation.
 Maps / Routing API                             Supplier GPS, distance/time computation and route optimisation.
                                                Reading customer POs (email / WhatsApp / physical) with
 OCR / AI
                                                confirm-on-mismatch; supplier documents subject to feasibility.
 Google Drive                                   Bulk product-image import with filename-to-item-code matching.
 AI Image Search & Recommendation               Wave 2 assisted image search and recommendation.

### B.5 ERPNext vs Custom — Development Overview
The build reuses ERPNext where it fits and adds custom components where the business need is not met
by defaults. Part F.4 gives the full customisation plan; the summary is:

 Delivered by ERPNext default (with
                                                Custom development required
 configuration)
 Customer, Supplier, Item, Warehouse,
                                                Series doctype and series-to-item auto-population.
 Batch, UOM, Price List.
 Quotation, Sales Order, Purchase
 Order, Purchase Receipt, Delivery              Dealer Inquiry / CRM doctype, duplicate detection and missed-demand
 Note, Sales Invoice, Payment Entry,            aggregation.
 Stock Entry.
 Payment Terms (for dealer payment
                                                Auto-generating Reorder Plan (scheduled job + review screen).
 policy).
 Roles & permissions, workflow
 engine, print formats, report builder,         Transporter & Vehicle master; Pickup/Route Plan and route optimisation.
 dashboards.
 Portal framework (basis for dealer
                                                Warehouse Bay master and bay-allocation logic; Bay Allocation Slip.
 app).
 REST API (basis for middleware /
                                                QR/sticker print formats and mobile scanning validation UI.
 integrations).
 Batch and stock ledger.                        Claim Voucher; Unloading Payment Voucher.
                                                Sample Request, Display Placement Slip, Sample Display Reconciliation and
 e-Way bill (via GST integration).
                                                product-withdrawal batch job.
                                                WhatsApp flow engine + reminder/response-button handling; dealer app;
 Notifications framework (email).
                                                India Banking, OCR and Google Drive integrations.

## Part C — Functional Requirements by Module

### C.1 Master Data Module
### C.1.1 Series-Driven Item Model
The Series is the core master concept. A Series is a manufacturer/company collection sharing a common
size, thickness, finish and price point. It carries an attribute section, a price section and — new in this
version — bulk-quantity and retail-quantity thresholds. When an item is created and a series is selected,
the item inherits the series attributes, pricing and bulk/retail thresholds automatically; only a small set of
values vary at item level. One series yields 20–25 items.

                                                Figure 2 — Series-to-item data model

  Key series rules
  A change in size or thickness creates a NEW series — a series never holds multiple sizes or thicknesses.
  Colour is variable and set at item level, not on the series.
  Finish is a two-level dropdown: a prime finish (Matt / Glossy) with sub-finishes (rocker, carving, stone-art,
  GVT/PGVT, etc.).
  All three price lists, and the bulk and retail quantity thresholds, are set on the series and flow to every item
  under it.

  Development note — customisation required
  Series is a custom doctype. Item creation must be driven by a client script / server hook that copies the
  selected series' attributes, price-list rates and bulk/retail thresholds onto the new Item and its child tables. This
  series-to-item propagation is custom (ERPNext has no Series concept).

### C.1.2 Item Attribute Dictionary
Item attributes are controlled dropdowns constituting the item:

 Attribute                                      Attribute                                 Attribute
 Size                                           Finish                                    Colour
 Base Colour                                    Thickness                                 Series
 UOM                                            Pieces per Sq Ft                          Weight per Box
 Batch                                          Lead Time                                 Status

Colour is captured as both Colour and Base Colour. Lead time exists at item level but is dynamic and
often cannot be pre-defined for manufactured tiles; procurement lead-time configuration is not part of
initial scope (Part G).

### C.1.3 UOM & Weight Handling
Box is the primary purchase and stock UOM. Pieces, Square Feet and Square Metre are conversions from
Box. Weight handling is a key requirement:

   •​ Every UOM carries a conversion factor; the system must convert freely between box, pieces,
      square feet, square metre and weight.
   •​ A standard weight is held at series/item level (per tile and per box); the actual, variable batch
      weight is captured at inward and overrides the standard for that batch.
   •​ Weight (in the correct UOM) must be displayed on every relevant document — inquiry, quotation,
      sales order, delivery note, invoice, purchase order, receipt, pickup plan, bay allocation slip and
      stickers.

  Development note — customisation required
  ERPNext supports UOM conversion on Item, but per-batch variable weight and the requirement to surface
  weight on all transaction print formats need custom fields (batch weight) and custom print-format logic. Add a
  batch-level weight field and a computed “total weight” on transaction lines and totals.

### C.1.4 Dealer / Customer Master
Dealers are classified on two independent axes; payment terms use the ERPNext default Payment Terms
mechanism.

 Classification                                 Values / Basis
                                                Standard Dealer / Dealer / Master Dealer, based on the dealer's sales.
                                                Confirmed initial bands (configurable): below ₹1 lakh → Standard Dealer; ₹1
 Price classification                           lakh and above → Dealer; ₹15 lakh and above → Master Dealer.
                                                Out-of-Bangalore dealers may receive Master Dealer pricing (MDP) to absorb
                                                higher transport cost.
                                                Retail / Bulk / Project / Distributor. Drives default retail-vs-bulk treatment: a
 Dealer type                                    bulk/project dealer defaults to bulk quantity even for a small order; a retail
                                                dealer defaults to retail.
                                                Configured using ERPNext's default Payment Terms / Payment Terms
 Payment policy                                 Template (credit vs advance). Special items may require 100% advance (see
                                                C.3.4).

Each dealer also carries their catalog visibility (see C.1.5) and, later, an assigned salesperson. Dealer
eligibility criteria and demotion are configurable business rules to be defined.

  Dealer classification bands (sales-based, configurable)
  Below ₹1 lakh → Standard Dealer
  ₹1 lakh and above → Dealer
  ₹15 lakh and above → Master Dealer
  The measurement basis (period / rolling window) is configurable; these thresholds are the confirmed starting
  values.

### C.1.5 Dealer Codes & Sample-Based Catalog Visibility
Dealers use their own product codes as well as Pacific Inc's company (product) codes. Both are stored so
a dealer can be recognised by either. This is central to inquiry, the dealer app and WhatsApp:

   •​ Each dealer's product codes are stored against the corresponding company item code in a
      dealer-code mapping (Item child table 'Customer Part Number' — see Part D).
   •​ A dealer can search and inquire using their own code OR the company product code; the system
      reverse-maps to the internal item.
   •​ A dealer can only access items for which a sample has already been issued to that dealer. Catalog
      visibility is therefore driven by the dealer's issued-sample list, not the full inventory.
   •​ For key dealers, code mappings are bulk-imported via templates (see F.3).

  Development note — customisation required
  Dealer-code identification (match on either dealer code or company code), sample-issued catalog gating, and
  reverse-mapping in inquiry/app/WhatsApp are custom. Implement a resolver service the middleware and
  WhatsApp/app call to turn any incoming code (or image) into an internal Item, scoped to the dealer's issued
  samples.

### C.1.6 Supplier Master
Suppliers (manufacturers/factories, mostly in Morbi, Gujarat) capture standard details plus GPS
coordinates and factory location, used for pickup and route planning (C.5). The insurance-holder
reference is captured for claims (C.8). A supplier may also be flagged as a transporter where applicable.

 Field                                          Purpose
 GPS coordinates (latitude, longitude)          Route optimisation and pickup sequencing.
 Factory location / address                     Pickup stop identification.
 Insurance holder / policy reference            Claims routed via the supplier's insurer (C.8).
 Material-ready contact                         Vendor enquiry and follow-up.

  Development note —
  customisation required
  Add custom fields for GPS
  coordinates (and optionally a

 Field                                          Purpose

  geolocation field) on Supplier;
  these feed the Maps/Routing
  integration.

### C.1.7 Transporter & Vehicle Master
Transporters and their vehicles are mastered for pickup and route planning on the Morbi (Gujarat) →
Bengaluru corridor for full loads. Vehicle type determines capacity and mode. The confirmed fleet is:

 Mode                                           Vehicle Type (full load)       Capacity
 Truck (by road)                                Truck                          43.50 Tonnes
 Truck (by road)                                Truck                          42.00 Tonnes
 Truck (by road)                                Truck                          41.00 Tonnes
 Truck (by road)                                Truck                          40.50 Tonnes
 Truck (by road)                                Truck                          35.00 Tonnes
 Truck (by road)                                Truck                          31.00 Tonnes
 Truck (by road)                                Truck                          25.00 Tonnes
 Container (rail + road)                        Container                      32.50 Tonnes
 Container (rail + road)                        Container                      31.00 Tonnes
 Container (ship + road)                        Container                      28.00 Tonnes
 Container (ship + road)                        Container                      31.00 Tonnes

Each vehicle type records its mode (road / rail+road / ship+road), full-load capacity in tonnes, indicative
transit time and standard rate basis. Rates are largely standard across transporters for a given vehicle
type. The capacity values drive utilisation and overload checks in pickup planning (C.5).

The transport master records, per vehicle, the owner, vehicle number, vehicle type and capacity,
alongside mode and indicative transit time. When a pickup is planned, the driver is notified of the route
and the sequenced stops (pickup points) and can view them through a shared link or the app (see C.5.1).

  Development note — customisation required
  Transporter is a custom master with a Vehicle child table (owner, vehicle_number, vehicle type, mode,
  capacity_tonnes, transit_days, rate basis). ERPNext's transporter (a Supplier flag) does not model vehicle-type
  capacity; a custom master is required.

### C.1.8 Warehouse & Bay Master
Initial scope is a single Bangalore warehouse; which consists of different bays. Each bay records code,
type, size, capacity, suitable category, current occupancy, available space, status and (for buffers) the
linked main bay. Bay types: Main, Buffer, Damage, Insurance Claim, Display/Sample and

Blocked/Reserved. Bay sizes (e.g. 36×6, 36×8, 32×6, 32×8 ft) are warehouse bay areas, not tile
dimensions.

  Client dependency
  The full list of bay sizes and bay types, and the warehouse design / layout and capacity, are to be provided by
  Pacific Inc and configured during setup (see F.9).

### C.2 Dealer Inquiry & CRM Module
### C.2.1 Inquiry Capture & Code Identification
Inquiries are captured in a structured workflow from WhatsApp, the dealer app, phone or internal entry.
Item identification accepts the dealer's own code, the company product code, or a product image, and
resolves to the internal item — scoped to items for which the dealer has been issued a sample. A
closest-match search (target ~85–90% confidence) is applied; on a mismatch or multiple candidates, the
user/dealer is prompted to confirm the exact code rather than the system guessing (this prevents the
variant-confusion errors seen in the past, e.g. one finish or sub-type mistaken for another).

Price quoted during an inquiry is not a flat list price: it is derived from the dealer's classification
(Standard / Dealer / Master Dealer) together with the dealer's purchase history to date, so a dealer sees
the price appropriate to their brand and relationship. The applicable price list is selected from the dealer
classification and adjusted per the dealer's history, and any change beyond the derived price is an
override requiring approval (C.11).

### C.2.2 WhatsApp Inquiry Flow
The WhatsApp flow is kept simple and aligned to how dealers already message today (per style, with
quantity in boxes/pieces, often quoting a code or sending an image). The logic is below, followed by
representative sample conversations.

                                                Figure 3 — WhatsApp dealer inquiry logic

  Batch suggestion in the inquiry reply
  When the item is available, the reply suggests the batch that can meet the required quantity.
  If no single batch holds enough, the system suggests a 2–3 batch combination that together fulfils the
  requirement.

Sample conversation — item in stock:

 Dealer message                                  System reply (WhatsApp)
                                                 Identifies item from dealer/company code or image (within issued samples).
 “White tiles 40 boxes available?” (or           Replies with product image, name, size, finish, the suggested batch (or a 2–3
 sends code / image)                             batch combination that meets the quantity), and price. Buttons: [Enquire
                                                 more] [Place order].
                                                 “Please share your PO number to confirm.” Dealer enters own PO number →
 Taps [Place order]
                                                 Sales Order created; confirmation sent.

Sample conversation — item out of stock:

 Dealer message                                  System reply (WhatsApp)
                                                 Identifies item; stock insufficient. Captures the inquiry and missed demand,
                                                 triggers reorder/vendor enquiry, and replies with expected lead time and a
 “Moon Onyx 150 boxes?”
                                                 suggested alternative. Buttons: [Notify when available] [See alternative]
                                                 [Close].

 Dealer message                                 System reply (WhatsApp)
                                                Reminder message with buttons: [Still need] [Received elsewhere] [Cancel].
 No response after follow-up window
                                                Outcome recorded against the inquiry.

  Development note —
  customisation required
  The WhatsApp conversational flow
  (menu logic, code/image
  resolution, template messages,
  buttons and reminder handling) is
  a custom middleware +
  flow-engine build against the
  WhatsApp/Meta API; it is not an
  ERPNext feature. Templates
  require Meta approval.

### C.2.3 Inquiry Status & Closure
Each inquiry carries a lifecycle status (Open, Available, Partially available, Out of stock, Pre-order
required, Quoted, Converted to order, Mapped to PO, Rejected, Closed). Closure is driven by the
customer PO: an inquiry is closed only after a customer PO number is linked to it, and ultimate closure is
on the connected sales invoice. Dropped inquiries record a reason, producing a clean
missed-opportunity / conversion record.

### C.2.4 Duplicate Inquiry & Duplicate PO Detection
The system detects repeat inquiries for the same dealer, item and quantity within a configurable window
(48 hours as a starting default) and prompts to confirm whether the inquiry continues an earlier one. It
similarly flags duplicate customer POs (same dealer, same quantity and material, different PO number)
so the same order is not reserved or procured twice.

### C.2.5 Missed Demand
Genuine repeated inquiries where stock is unavailable — for example several inquiries for the same
design from different dealers, with quantity variation above a configurable threshold — contribute to
missed-demand analysis and surface on the reorder plan as a suggestion.

### C.2.6 Follow-up via WhatsApp (No Calling)

  Follow-up is WhatsApp-only
  There are no calling notifications and no automated voice calls.
  Dealer follow-up: unresolved inquiries send WhatsApp reminders with response buttons (e.g. [Still need]
  [Received elsewhere] [Cancel]); the dealer's tap is recorded and updates the inquiry.
  Staff CRM follow-up: WhatsApp reminders are also triggered to staff for pending CRM actions, with response
  buttons to update status.
  Reminder cadence is configurable (e.g. up to three within a week).

### C.3 Sales & Order Management Module

### C.3.1 Order Lifecycle & Reservation
A customer PO becomes an internal Sales Order. If stock is available it converts to delivery and invoice; if
not, it drives a purchase order, inward and bay placement before delivery. Delivery date is mandatory on
the Sales Order and can be maintained item-wise for multi-item orders. Stock is reserved on
confirmation, but long-duration commitments are reviewed and can be released so material is not
blocked idle — the system distinguishes a customer's delivery commitment from actual physical stock
availability. Weight (UOM-converted) is shown on every sales document.

                                         Figure 4 — Order-to-cash lifecycle with reservation

### C.3.2 Supplier vs Customer Timelines

Two timelines are maintained separately: the supplier/factory commitment date and the customer's
committed delivery date. Supplier delays are compared against the customer commitment; only where a
delay threatens the customer date does the system flag the impact and trigger escalation and dealer
notification. A forecasting/stock window shows, per order and item, stock-in-hand, in-transit and
factory-dispatch status, groupable by customer, item or supplier.

### C.3.3 Bill-to / Ship-to, Drop-ship & e-Way Bill
Bill-to and ship-to are mandatory and validated, particularly for interstate and site deliveries. Drop-ship is
supported: a Sales Order links to a purchase order carrying the customer's bill-to/ship-to, the supplier
ships directly to the customer/site, and the invoice carries the correct addresses. The system reads pin
codes, computes distance and automatically generates e-Way bills for interstate and threshold-crossing
consignments.

                                            Figure 5 — Drop-ship and bill-to / ship-to flow

### C.3.4 Advance Payment & Dispatch Control
Payment terms are configured with ERPNext Payment Terms at dealer level. Cash/advance dealers
typically pay an advance against order and 100% before dispatch; special items may require 100%
advance. Where an advance condition applies, dispatch/loading is blocked until the payment condition is
satisfied — the receipt, reflected via the India Banking / VALS API, acts as the release trigger, so goods do
not load until the system shows the required payment.

  Development note — customisation required
  The dispatch-lock (block Delivery Note / gate-out until the required advance is confirmed via VALS API) and the
  supplier-delay impact check are custom automations layered on ERPNext's Payment Terms and Sales Order.

### C.4 Purchase & Reorder Planning Module
### C.4.1 Reorder Planning — Auto-Generated, Review-Only

  The reorder screen is a review window, not a data-entry screen
  Suggested purchase quantities are generated automatically by the system from historical retail sales and stock
  position; the purchase team does not type them in.
  The team reviews the auto-generated plan and adjusts quantity or frequency only where needed before
  creating POs.

A scheduled job computes the plan from retail sales over the last 3–6 months (excluding bulk/project
sales), current stock, reserved quantity, in-transit quantity, pending quantity and missed demand. The
suggested quantity targets a configurable stock level (≈ three months mandatory, orders typically for two
months), is rounded up to the nearest 5 boxes and checked against the applicable MOQ.

                               Figure 6 — Purchase-planning automation (auto-generate → review)

  Reorder formula (configurable)
  Net available = Current stock − Reserved + In-transit
  Suggested reorder = Target stock (≈ N months of avg retail sales) − Net available + Pending/missed demand
  Round UP to nearest 5 boxes; then apply MOQ (company / vendor / production).

The review screen shows, per item: average sales, current stock, reserved, in-transit, available, pending,
missed demand, MOQ, suggested reorder quantity, item-level reorder frequency, supplier/item-group
filter, active/discontinued status, last purchase date and lead time.

When the scheduled job generates a draft reorder plan, the responsible members (purchase team /
reorder owner) are notified to review it, so an auto-generated draft is always actioned rather than left
unseen.

  Development note — customisation required
  The reorder auto-calculation (scheduled job), the Reorder Plan doctype and its review screen are custom.
  ERPNext's native reorder level is too simple for this multi-factor, retail-only, round-up, MOQ-aware logic.

### C.4.2 MOQ Hierarchy & Vendor Enquiry
MOQ may apply at company, vendor/manufacturer and item level, and production MOQ may differ from
stock MOQ. When a requirement is below MOQ, the system prompts to raise to the applicable MOQ. A
vendor enquiry determines whether material is available from ready stock or must be produced,
capturing expected readiness and available quantity. The indicative 80-box company MOQ is not treated
as universal until confirmed per item/category.

### C.4.3 Retail vs Bulk Classification
Every invoice is tagged retail or bulk. Classification is rule-based at series and size level: each series
carries a bulk-quantity and a retail-quantity threshold (see C.1.1 / Part D), and dealer type also drives the
default (bulk/project dealer → bulk). Only retail sales drive reorder suggestions; bulk/project sales are
excluded from reorder and from top-dealer/sample logic. Auto-classification applies, with an
audit-locked manual override for exceptions.

### C.4.4 Purchase Receipt & Shortage Handling
Purchase receipt captures supplier, PO, item, quantity received, batch (with variable weight), shortage
and damaged quantity, bay allocation, QR generation, LR number and supplier invoice reference. On a
shortage, responsibility is recorded (factory in most cases, sometimes the driver, otherwise absorbed), a
claim or credit/debit note is initiated, and the shortage is linked to the PO and LR.

### C.5 Supplier Pickup & Route Planning Module
Supplier GPS coordinates (C.1.6) are used to plan pickups on the Morbi→Bengaluru corridor. The system
selects between container and truck (C.1.7 fleet) and optimises the route. Container is cheaper but
slower (rail/ship + road, ~15–20 days, up to ~3 factories per trip); truck is faster (~5–6 days, up to ~9–10
factories) but more expensive. Planned orders (new launches, reorders, large projects) prefer container;
urgent/on-demand orders use truck. For price-list purposes the maximum (truck) pricing is used as the
base.

                                      Figure 7 — Route and vehicle (container/truck) planning

  Route optimisation rule
  The system provides BOTH time-optimised and distance-optimised routing, and presents both so the planner
  can choose.
  Real-time traffic and toll preference are not decision factors at this stage.
  Capacity planning uses the fleet tonnages (C.1.7) with utilisation and overload warnings; up to ~30–40 GPS
  stops can be sequenced. Plans are shareable with drivers.

  Development note — customisation required
  Pickup/Route Plan doctype, the container-vs-truck selection logic, capacity/overload checks and the
  Maps/Routing API integration (time and distance options) are custom.

### C.5.1 Vehicle Tracking & Stop Capture
Once a pickup plan is dispatched, the system tracks which truck is where and what it has picked. Live GPS
gives the truck's current location; at each supplier stop the system captures arrival time, the items picked
(quantity and weight), the dwell time (how long the truck stayed at that supplier) and the departure
time. A consolidated view shows, for each truck, its current location, what has been picked so far and the
remaining stops. The driver receives the route and sequenced pickup points on assignment and can view
them through a shared link or the app.

                                        Figure 7b — Vehicle tracking and stop/dwell capture

  Development note — customisation required
  Live vehicle tracking, per-stop pickup capture and stop/dwell-time capture are custom, built on the
  Maps/Routing integration and the Pickup/Route Plan; a tracking log holds location pings and stop events.
  Confirm the GPS source (driver-app location vs a vehicle GPS device) during setup.

### C.6 Inventory: Batch, Bay, QR & Scanning Module
### C.6.1 Batch Tracking & Variable Weight
Weight is maintained at batch level and is variable — a standard weight is held on the series/item, and
the actual batch weight (which changes when the manufacturer changes materials) is entered when
inventory is received. Batches are created at inward, not at PO stage, and the batch value includes the
manufacturer's date of manufacturing (the company uses DoM as its batch). For a repeat PO, the system
can reference the last received batch's weight. Weight is carried through and displayed on all
downstream documents.

### C.6.2 Batch Visibility & Top-3 Batches
Stock is traceable at batch level. For practical dealer-facing visibility, the system consolidates and shows
the top-3 batches by available quantity, with drill-down to full batch detail on request. The exact
dealer-facing batch-sharing rule is configurable (some dealers demand single-batch supply, which the
business wants to control rather than expose by default).

### C.6.3 Bay Allocation Logic
Bay allocation is system-suggested and warehouse-confirmed. The detailed logic is shown below:
incoming material is first checked for damage/claim routing; otherwise the system determines the
suitable bay type for the item's category, checks main-bay space and capacity, falls back to a linked buffer

bay (or a main+buffer split) on overflow, consolidates open/partial quantities and avoids style/batch
mixing, then produces a bay allocation slip for confirmation before unloading; occupancy is recomputed
after the QR scan update.

                                                    Figure 8 — Bay allocation logic

  Development note — customisation required
  Warehouse Bay master, the bay-suggestion algorithm (category, batch, size, occupancy, buffer linkage,
  open-box consolidation) and the Bay Allocation Slip doctype are custom.

### C.6.4 QR / Barcode Generation, Stickers & Printing
Two sticker types are generated. The box / inward sticker is printed per box at purchase receipt (printing
handled by the purchase team) and encodes the company item code, product name, size/finish/series,
batch (including manufacturing date), bay, boxes/pieces, weight, and PR and supplier references. The
dealer sample sticker (see C.10) carries a dealer-specific unique code, the human-readable product
name, series/size/finish and the sample batch. Both are QR-based; barcode fallback is supported. The
sticker field layouts are below.

                                  Figure 9 — Box/inward sticker and dealer sample sticker formats

  Development note — customisation required
  Sticker layouts are custom Print Formats; QR generation and the per-box print run (batch printing from a
  purchase receipt) are custom. Confirm label size and printer/scanner hardware during setup.

### C.6.5 Warehouse Mobile Scanning Interface
The warehouse team uses a mobile web interface (and PDA) to scan and validate throughout. The user
selects a task — inward, bay placement, pick (against a pick list) or dispatch — scans the QR/barcode,
and the system validates item, batch, bay, quantity and pick list. A match confirms and updates
stock/bay/pick; a mismatch alerts and blocks the action. When a pick list is complete, dispatch validation
gates the material out.

                                  Figure 10 — Warehouse mobile scanning, pick list and dispatch

  Development note — customisation required
  The mobile scanning UI (task selection, scan-validate-confirm, pick-list execution and dispatch gate) is a custom
  mobile web interface calling ERPNext stock APIs; it is not the ERPNext default desk UI.

### C.6.6 Loose / Unallocated Stock Tracking
Sometimes a single tile or box is set down in an empty space that is not an allocated bay. This must still
be tracked so stock does not go invisible. Using the mobile scanning interface, the item and the ad-hoc
location are scanned and recorded as loose / unallocated stock (item, batch, quantity, location); it then
appears on an unallocated-stock check/report, from which the warehouse either allocates it to a proper
bay (bay allocation slip) or consolidates it with existing open/partial stock. A periodic check helps ensure
nothing remains untracked in unallocated space.

                                          Figure 10b — Loose / unallocated stock tracking

  Development note — customisation required
  Loose/unallocated stock capture (scan item + ad-hoc location), the unallocated-stock report and the
  allocate/merge flow are custom, layered on the scanning UI and Bay master.

### C.7 Pricing Engine Module
### C.7.1 Series Price Setting
Pricing is a first-time activity performed at series level. The landing cost is built from factory-side
components (basic rate per box, insurance %, freight, loading) and the margin is applied to derive the
three price lists (Standard Dealer, Dealer, Master Dealer) and MRP. Insurance % is configurable and
standardised per series. Margin varies by series (premium series carry higher margin). The build-up is
retained as a price working for reference and audit.

At the point of quoting (inquiry or quotation), the price list applied is selected from the dealer's
classification (Standard / Dealer / Master Dealer per the sales-based bands in C.1.4) and adjusted for the
dealer's purchase history to date, rather than a single flat list price.

  Pricing formula (configurable)
  Landing Cost = Basic Rate + Freight + Loading + Insurance + Other landed costs
  Suggested Dealer Price = Landing Cost + Margin %
  Three price lists derive from the series margin structure; for price-list purposes the maximum (truck) freight is
  used.

### C.7.2 Quotation Price Working & Retail Markup
Quotations use a separate per-item price working, so a project or negotiated quotation can rework
freight (container vs truck), margin and discount without disturbing the master price list. The working
shows a clear bifurcation (with/without GST, with/without transport) and supports a transport
dropdown by factory-to-location. Retail customer quotations may apply a markup percentage over the
approved dealer price as a quotation-level override only.

### C.7.3 Discounts
Discount is not assumed automatically from dealer identity; the default is the dealer's applicable price
list. Any transaction-level price or discount change — even a one-rupee change — is an override
requiring approval and notification (C.11).

  Development note — customisation required
  The series price-setting calculator and the quotation-level price working (with cost build-up and GST/transport
  bifurcation) are custom. ERPNext Price Lists hold the resulting rates, but the calculation UI and the
  per-quotation rework are custom.

### C.8 Damage & Insurance Claims Module
Damage may occur in transit, at unloading, during inspection or during warehouse movement. Damaged
goods move to a damage bay and, where applicable, to an insurance claim bay. Because individual

damage values are small (a commonly accepted quantum per truckload), small values are accumulated
— always grouped by supplier/company — before a claim voucher is raised.

                                           Figure 11 — Damage and insurance claim flow

### C.8.1 Claim Voucher & Settlement
A claim voucher records claimed, approved and received amounts and net loss, with linked
supplier/consignment/invoice references and supporting documents. Settlement is by one of: insurance
(the vendor holds the policy and passes settlement via a credit note to Pacific Inc), vendor credit note,
partial settlement, or non-settlement / write-off. Escalation reports track pending insurance follow-up.

### C.8.2 Accounting Treatment & Write-off
A claim voucher is not accepted for tax purposes, so it is held operationally and reconciled: removed by a
journal at year-end (31 March) and reinstated on 1 April, and cancelled when the corresponding credit
note is received. Material determined unusable is written off from stock (rather than remaining in
damages) and handled via salvage or disposal. Transit damage from warehouse to customer is Pacific
Inc's responsibility and handled by credit note. The operational claim record and its accounting
treatment stay linked throughout.

### C.8.3 Shortage Claims
Shortages identified at receipt (C.4.4) feed the same claim/credit-note mechanism, with recorded
responsibility (factory / driver / absorbed).

  Development note — customisation required

  Claim Voucher doctype, supplier-wise accumulation, the settlement-mode handling and the year-end journal
  reconciliation cycle are custom on top of ERPNext's credit/debit note and stock write-off.

### C.9 Unloading & Labour Payments Module
### C.9.1 Unloading Payments
Unloading and labour charges are captured against the consignment/LR: labour/vendor, amount
payable, payment mode (PhonePe, Google Pay, UPI, NEFT, bank transfer, cash), paid/unpaid status,
transaction reference, linked purchase receipt and supporting attachment. Payment vouchers are
generated per consignment. Automatic payout is not included unless separately confirmed; the module
tracks and records payments.

  Development note — customisation required
  Unloading Payment Voucher doctype and LR-wise linkage to purchase receipts are custom.

### C.9.2 Labour Attendance & Payment
Beyond consignment-linked unloading charges, the system tracks labourers directly: a labour attendance
and payment record captures each labourer, days/shifts present, work done (e.g. unloading, movement),
the amount due and the amount paid, with payment mode and reference. This gives a running view of
labour attendance and dues, and links to the unloading vouchers where the work was
consignment-related.

  Development note — customisation required
  Labour Attendance & Payment is a custom doctype (labourer master reference, attendance entries, computed
  dues and payments). It is not covered by ERPNext defaults; if formal HR payroll is later required it can move to
  the ERPNext HR module.

### C.10 Sample & Display Management Module
### C.10.1 Sample Approval & Dealer-Specific QR
Sample movement requires approval: a sample request is created, approved, and only then moved out
(through a proper stock movement or invoice), after which it leaves the pending list. Each sample carries
a dealer-specific unique QR (see the sample sticker in Figure 9) — the same product placed with different
dealers gets different codes (a per-dealer prefix/suffix over the item code) — while remaining
human-readable by product name. Samples retain the batch they were issued from for traceability, but
that batch does not force the same batch on a future sale.

### C.10.2 Sample Placement Automation & Monitoring
When a sample is issued, a Display Placement Slip is auto-created (capturing item, dealer/location,
quantity, placement date and a photo); the placement date starts the monitoring clock. Configurable
reminders (e.g. 3 / 6 / 12 months) are sent by WhatsApp and email with response buttons, and periodic
reconciliation identifies displays needing review, replacement, return or follow-up.

                                    Figure 12 — Sample placement automation and monitoring

### C.10.3 Sample Display Reconciliation
A dedicated Sample Display Reconciliation doctype records the outcome of each monitoring cycle for
sample/display stock held at dealer locations. The dealer (or staff) responds via WhatsApp buttons —
[Still displayed] / [Removed] / [Loose / not shown] — and the response updates the reconciliation
record. This keeps the display stock at dealer locations reconciled rather than assumed, and drives the
removal + put-up list (replace a slow-moving displayed design with a newer design not yet displayed).

  Development note — customisation required
  Sample Request, Display Placement Slip and Sample Display Reconciliation are custom doctypes. The
  placement-slip auto-creation, the monitoring scheduled job, reminder dispatch and WhatsApp button handling
  are custom automations.

### C.10.4 Top Dealer Logic & Pullback
Top dealers for sample/display planning are identified from retail sales over the last six months,
excluding bulk/project sales. On discontinuation or removal, displays are pulled back to the warehouse
with returned-condition tracking and a decision to resell, write off or move to clearance.

### C.10.5 Product Withdrawal Automation (Discontinued / Pacific-Discontinued)
A daily batch job scans every item and moves it through the lifecycle Active → PDC → Against Order →
Discontinued based on configurable, series-level criteria: displayed for at least a configurable number of
months in at least a configurable number of stores, AND annual retail sales below the series threshold.
PDC (Pacific-discontinued) means the item is not replenished and hidden from dealers; Against Order
means it is ordered only on specific demand; Discontinued means the factory has stopped it

permanently. Reaching a withdrawal state auto-triggers the display removal + put-up list, pullback to
warehouse and an alternate-product suggestion. The threshold is set at series level while the status
change is confirmed at item level.

                                            Figure 13 — Product withdrawal automation

  Development note — customisation required
  The daily withdrawal batch job (display-duration, store-count and annual-sales checks), the state transitions
  and the auto-triggered removal/put-up/pullback actions are custom scheduled automations.

### C.11 Approvals & Workflow Module
The system uses a submit-freeze model: a document is freely editable in draft and frozen on submission.
Certain fields (e.g. delivery date) can be edited by authorised users without cancelling; a full amendment
or cancellation produces a version diff and routes for approval. The owner is notified of critical actions
(e.g. cancellations, quantity reductions on a PO already sent to a vendor) even when a delegate acts.

                                         Figure 14 — Approval and submit-freeze workflow

  Override triggers requiring approval
  Credit-limit exceedance and overdue outstanding.
  Any pricing override (even a one-rupee change) and any discount over the default price list.
  Amendment or cancellation of a submitted sales/purchase document.
  Retail-vs-bulk classification override (audit-locked, authorised user only).
  Notifications for the above are delivered via WhatsApp, email, in-app and SMS, with mobile approval.

  Development note — customisation required
  The six override triggers, the amend/cancel version-diff routing and the owner-notification-on-critical-action
  are custom, built on ERPNext's workflow engine and notifications.

### C.12 Reporting, Dashboards & Assisted AI Module
### C.12.1 Action-Oriented Dashboards

Dashboards surface exceptions and initiate action. Core linkages: Short Stock → Reorder, Pending Inquiry
→ Follow-up, Supplier Delay → Impact Check, Sample Ageing → Review/Replacement, Damage →
Claim, and Pending Payment → Dispatch Restriction. Role dashboards (CEO, Sales, Purchase,
Sample/Display, Finance) roll up into a management view with monthly/yearly performance, top sellers,
top designs, top supplier brands, and top/low-performing dealers with corrective prompts.

### C.12.2 Report Set
 Report                                                        Report
 Inquiry register (CRM)                                        Open / pending inquiry
 Duplicate inquiry                                             Missed demand / opportunity
 Inquiry-to-PO mapping                                         Inquiry conversion
 Reorder planning                                              PO pending
 Purchase pickup plan                                          Bay occupancy
 Visual stock balance                                          Batch-wise & top-3 batch stock
 Consolidated stock                                            Fragmented quantity
 Stock movement                                                Stock ageing
 Short / insufficient stock                                    Retail vs bulk
 Damage & transit damage                                       Claimable value / insurance claim
 Unloading payment                                             Pricing
 Fast / slow-moving product                                    Stock clearance suggestion
 Display placement & ageing                                    Display replacement / put-up
 Dealer & salesperson performance                              Advance payment / pending-before-loading

### C.12.3 Salesperson Assignment
A salesperson-to-dealer assignment is to be introduced, with targets and performance reporting,
supporting ownership of dealer relationships and incentives.

### C.12.4 Customer PO OCR & Product Matching
Customer POs arriving by email, WhatsApp, physical document or a customer system are read via OCR/AI
to extract PO number, items and quantities. The system does not silently accept an uncertain match: at a
target confidence (~85–90%) it auto-matches, and on ambiguity or multiple candidate items it prompts
the user to confirm the exact item. Supplier email/document reading is a candidate capability subject to
feasibility and format validation.

### C.12.5 Assisted AI (Wave 2)
Wave 2 adds assisted AI: image-based product search (dealer sends an image; the system matches
catalog items and suggests similar/alternative products by colour, finish, pattern, tone, size, design family
and available stock), a rule-based-plus-AI recommendation engine for out-of-stock and alternatives, and
assisted forecasting on retail sales, inquiries, missed demand, stock ageing and lead time. Accuracy
depends on image quality and tagging; an enterprise-grade accuracy guarantee is not in scope.

### C.12.6 Future Customer API Integration
For larger dealers running their own ERP, a future API integration would let a dealer's PO transfer into
Pacific Inc's system. An interim approach converts a dealer inquiry directly to an order where the dealer
enters their own PO number to tag and close it. Feasibility and specifications remain to be evaluated.

### C.13 Web Dealer App — Enquiry Module
The web dealer app is a browser-based portal (desktop and mobile) that complements WhatsApp. Its
enquiry module is central and behaves consistently with the middleware's dealer-wise rules. The dealer
flow is below.

                                                Figure 15 — Dealer app enquiry flow

### C.13.1 Enquiry Module Behaviour
   •​ Login is dealer-specific; the dealer sees only items for which a sample has been issued to them.
   •​ The catalog shows each item with its images (product, application/room and additional images —
      see C.14) and, where enabled, price.
   •​ Search accepts the dealer's own code or the company product code; both resolve to the internal
      item.
   •​ The item detail shows images, real-time stock, the top-3 batches and price (if enabled for that
      dealer).
   •​ If in stock, the dealer can convert the enquiry to an order, entering their own PO number to tag
      and close it.
   •​ If out of stock, the dealer raises an inquiry / out-of-stock request and sees alternative and
      recommended items.
   •​ The dealer tracks order and delivery status, dues/outstanding, and order/inquiry history, and
      receives follow-up and status notifications.

  Development note — customisation required
  The dealer app (portal pages, dealer-scoped catalog, code-resolver search, enquiry-to-order with dealer PO,
  status/dues/history and notifications) is a custom portal build on ERPNext's website/portal framework, sharing
  the middleware's dealer-visibility rules with the WhatsApp channel.

### C.14 Catalog & Price-List Formats for Dealers
### C.14.1 Dealer Catalog Format (with Images)
A dealer-facing catalog is generated per dealer, restricted to the items for which that dealer has been
issued samples. Each catalog entry carries the item's images — a primary product image, an
application/room image and additional images — with the item's key attributes. The catalog can be
produced with or without price (price-inclusive or price-exclusive versions), and shared through the
dealer app and as a shareable/exportable format (e.g. PDF) for WhatsApp.

### C.14.2 Price-List Formats
Price-list formats are produced for sharing with dealers, presenting the applicable dealer price list
(Standard / Dealer / Master Dealer) and, where required, MRP. As with the catalog, price lists are
dealer-scoped so a dealer sees only their applicable prices and permitted items.

  Development note — customisation required
  Dealer-scoped catalog and price-list print/export formats (with the image set and price-inclusive /
  price-exclusive variants) are custom Print Formats / portal views driven by the dealer's issued-sample list and
  price classification.

## Part D — Data Structures (Custom Doctypes)
This part specifies the custom, non-ERPNext document types. ERPNext-native doctypes are reused
where possible (D.1). Field types follow Frappe conventions (Data, Link, Select, Int, Float, Currency,
Percent, Date, Datetime, Check, Attach, Table, Text, Small Text). Child tables are listed under their parent.

### D.1 ERPNext-Native Doctypes (Reused, with Customisation)
 ERPNext Doctype                                Customisation for Pacific Inc
                                                Link to Series; auto-populate attributes/price/bulk-retail thresholds on series
                                                select; item-level colour, base colour, finish, status; child tables for Image
 Item
                                                Gallery and Customer Part Number (dealer codes); batch variable weight;
                                                weight shown on print formats.
                                                Price classification, dealer type, turnover band, location (in/out Bangalore),
 Customer (Dealer)
                                                assigned salesperson, sample-issued catalog scope.
 Payment Terms / Payment Terms                  Used as-is for dealer payment policy (credit vs advance); linked at dealer
 Template                                       level.
                                                GPS coordinates, factory location, insurance-holder reference, is-transporter
 Supplier
                                                flag.
                                                Single Bangalore warehouse; batch-level variable weight and date of
 Warehouse / Batch
                                                manufacturing.
                                                Per-item price working (child), retail markup, with/without GST & transport
 Quotation
                                                bifurcation, weight display.
                                                From customer PO; mandatory item-wise delivery date; reservation;
 Sales Order
                                                drop-ship flag; supplier vs customer timeline; weight display.
                                                Material-ready status, pickup fields, shortage/damage capture, QR
 Purchase Order / Receipt
                                                generation at receipt, batch weight.
 Delivery Note / Sales Invoice                  Bill-to/ship-to, drop-ship, auto e-Way bill, weight display.
                                                Advance capture, India Banking (VALS API) real-time reflection,
 Payment Entry
                                                dispatch-release trigger.
 Stock Entry                                    Bay-to-bay movement with traceability and weight.
                                                Three dealer price lists; Box primary UOM with Pieces / Sq Ft / Sq Metre and
 Price List / UOM
                                                weight conversions.

### D.2 Master Doctypes (Custom)
Series (master — attribute + price + bulk/retail)
The central master. One series yields 20–25 items and pre-sets attributes, pricing and bulk/retail
thresholds.

 Field                            Type                  Req.        Notes
 series_name / series_code        Data                     Yes      Name and unique code.

 supplier                         Link (Supplier)          Yes      Manufacturer / company.

 Field                            Type                  Req.        Notes
 item_group                       Link                         No   Brand / grouping.

 size                             Link/Select              Yes      e.g. 600×600. A change = new series.

 thickness                        Select                   Yes      e.g. 5/9/12/15/16/20 mm. A change = new series.

 finish / sub_finish              Link                   Yes / No   Matt/Glossy → rocker, carving, stone-art, GVT/PGVT.

 pieces_per_box                   Int                      Yes      Conversion basis.

 weight_per_tile /
                                  Float                    Yes      Standard weights (kg); batch weight overrides at inward.
 weight_per_box

 height_mm / width_mm /
                                  Float                  Yes/No     Dimensions and derived area.
 sqft_per_box

 uom_conversions                  Table                    Yes      Box→Pieces/Sqft/Sqm and weight.

 bulk_qty_threshold               Int                      Yes      At/above this (per size) a sale is bulk.

 retail_qty_threshold             Int                          No   Retail band basis.

 annual_sales_threshold_b
                                  Int                          No   Configurable withdrawal (PDC) threshold.
 ox

 reorder_frequency                Select                       No   Reorder cadence.

 status                           Select                   Yes      Active / PDC / Against Order / Discontinued.

 basic_rate_per_box               Currency                 Yes      Price section.

 insurance_pct / freight /
                                  Percent/Currency             No   Landed-cost components.
 loading

 margin_pct                       Percent                  Yes      Series margin.

 price_list_rates                 Table                    Yes      Standard / Dealer / Master Dealer rates.

 mrp                              Currency                     No   For price-inclusive catalog.

Transporter (master (+ Vehicle child))
Transporters and the Morbi→Bengaluru full-load fleet for pickup/route planning.

 Field                            Type                  Req.        Notes
 transporter_name                 Data                     Yes      Transporter / agency.

 contact                          Data                         No   Contact details.

 vehicles                         Table (Vehicle)          Yes      Child table (below).

Vehicle child table:

 Field                            Type                  Req.        Notes
 owner_name                       Data                         No   Vehicle owner.

 vehicle_number                   Data                     Yes      Registration number.

 vehicle_type                     Data                     Yes      e.g. Truck / Container.

 Field                            Type                  Req.        Notes
 mode                             Select                   Yes      Road / Rail+Road / Ship+Road.

                                                                    Full-load capacity (see C.1.7 fleet: 43.5/42/41/40.5/35/31/25
 capacity_tonnes                  Float                    Yes
                                                                    T trucks; 32.5/31 T rail containers; 28/31 T ship containers).

 transit_days                     Int                          No   Indicative transit time.

 standard_rate_basis              Currency/Data                No   Largely standard by vehicle type.

 driver_name /
                                  Data                         No   Assigned on a pickup plan.
 driver_mobile

Warehouse Bay (master)
Bay layout and capacity for the Bangalore warehouse.

 Field                            Type                  Req.        Notes
 bay_code / bay_name              Data                   Yes/No     Identifier and readable name.

 warehouse                        Link (Warehouse)         Yes      Parent warehouse.

                                                                    Main / Buffer / Damage / Insurance Claim / Display /
 bay_type                         Select                   Yes
                                                                    Blocked.

 bay_size                         Select/Data                  No   e.g. 36×6, 36×8, 32×6, 32×8 ft (area).

 capacity /
 current_occupancy /              Float                        No   Capacity management.
 available_space

 suitable_category                Link/Data                    No   Product type best suited.

                                  Link (Warehouse
 linked_main_bay                                               No   Buffer→main association.
                                  Bay)

 status                           Select                   Yes      Active / Blocked / Reserved.

### D.3 Transactional Doctypes (Custom)
Dealer Inquiry (transactional (+ Item child))
Structured CRM inquiry; closes on PO linkage.

 Field                            Type                  Req.        Notes
 inquiry_no / inquiry_date        Data/Date                Yes      Reference and date.

 dealer                           Link (Customer)          Yes      Inquiring dealer.

 source                           Select                   Yes      Phone / WhatsApp / App / Internal.

 salesperson                      Link (User)                  No   Assigned owner.

 status                           Select                   Yes      Open … Converted / Mapped to PO / Rejected / Closed.

 follow_up_date /
                                  Date/Int                     No   Follow-up (WhatsApp) tracking.
 reminders_sent

 duplicate_flag                   Check                        No   Set by duplicate detection.

 Field                            Type                  Req.        Notes
 linked_customer_po /
                                  Data/Link                    No   PO closes the inquiry; SO on conversion.
 linked_sales_order

 closure_reason                   Select/Text                  No   Reason if dropped.

                                  Table (Inquiry                    Child: item, matched_code, image, req_qty,
 items                                                     Yes
                                  Item)                             availability_status, alt_item, missed_demand_value.

Bay Allocation Slip (transactional (+ Item child))
Produced before unloading; confirmed by warehouse.

 Field                            Type                  Req.        Notes
 slip_no / reference_doc          Data/Link                Yes      Reference and source receipt/order.

 warehouse_person                 Link (User)                  No   Responsible.

 confirmation_status              Select                   Yes      Suggested / Confirmed / Modified.

                                                                    Child: item, batch, qty, weight, suggested_bay, alternate_bay,
 items                            Table                    Yes
                                                                    buffer_bay.

Reorder Plan (transactional (+ Row child))
Auto-generated by scheduled job; purchase team reviews (review-only screen).

 Field                            Type                  Req.        Notes
 plan_date                        Date                     Yes      Run date.

 supplier_filter /
                                  Link                         No   Scope filters.
 item_group_filter

 generated_by_job                 Check                        No   Marks system-generated rows.

                                  Table (Reorder
 rows                                                      Yes      Child fields below.
                                  Row)

 — item                           Link (Item)              Yes      Stock item.

 — avg_sales                      Float                        No   Retail sales 3–6 mo (bulk excluded).

 — current_stock / reserved
 / transit / pending /            Float                        No   Availability & demand.
 missed_demand

 — moq / suggested_qty            Int/Float                    No   MOQ and auto-suggested qty (rounded up to 5).

 — reorder_frequency /
 last_purchase_date /             Mixed                        No   Planning context.
 lead_time / status

Pickup / Route Plan (transactional (+ Stop child))
Supplier pickup sequencing and vehicle planning.

 Field                            Type                  Req.        Notes
 plan_no / plan_date              Data/Date                Yes      Reference and date.

 transporter / vehicle_type       Link/Select                  No   From Transporter master.

 capacity_tonnes                  Float                        No   From selected vehicle.

 optimization_mode                Select                       No   Time / Distance (both offered).

 total_boxes / total_weight
                                  Float                        No   Load rollups; overload warning.
 / remaining_capacity

                                                                    Child: sequence, supplier, gps, po_ref, ready_qty, picked_qty,
                                  Table (Pickup
 stops                                                     Yes      weight, arrival_time, departure_time, dwell_time, contact,
                                  Stop)
                                                                    remarks.

Claim Voucher (transactional (+ Item child))
Damage / insurance / shortage claim with settlement.

 Field                            Type                  Req.        Notes
 voucher_no / claim_type          Data/Select              Yes      Transit / Shortage / Insurance.

 supplier                         Link (Supplier)          Yes      Grouping is supplier-wise.

 claimed / approved /
 received_amount /                Currency                     No   Settlement figures.
 net_loss

 settlement_mode                  Select                       No   Insurance / Vendor Credit Note / Partial / Write-off.

 linked_credit_note               Link                         No   On settlement.

 accounting_status                Select                       No   Held / Reconciled (31-Mar/01-Apr) / Cancelled.

 documents                        Attach                       No   Supporting docs.

 items                            Table                    Yes      Child: item, batch, qty, value, invoice/consignment ref.

Unloading Payment Voucher (transactional)
LR-wise unloading / labour payment.

 Field                            Type                  Req.        Notes
 voucher_no / lr_number           Data                     Yes      Reference and consignment LR.

 reference_doc                    Link (PR)                    No   Linked receipt.

 labour_vendor / amount           Data/Currency            Yes      Payee and amount.

 payment_mode                     Select                   Yes      PhonePe / GPay / UPI / NEFT / Bank / Cash.

 status / txn_ref                 Select/Data            Yes/No     Paid / Unpaid; UPI/bank reference.

 paid_by / payment_date /
                                  Link/Date/Attach             No   Audit and proof.
 attachment

Sample Request (transactional)

Sample approval and dealer-specific QR.

 Field                            Type                  Req.        Notes
 request_no                       Data                     Yes      Reference.

 item / qty                       Link/Float               Yes      Sample item and quantity.

 dealer_location                  Link (Customer)          Yes      Destination dealer/location.

 requested_by /
                                  Link/Date                Yes      Origin.
 request_date

 approval_status / approver       Select/Link              Yes      Approval.

 batch                            Link (Batch)                 No   Traceability (non-binding on sale).

 sample_qr_code                   Data                         No   Dealer-specific unique code.

Display Placement Slip (transactional)
Auto-created on sample issue; triggers monitoring clock.

 Field                            Type                  Req.        Notes
 slip_no / source_invoice         Data/Link              Yes/No     Reference; captured from invoice/movement.

 item / dealer_location /
                                  Link/Float               Yes      Design, location, quantity.
 display_qty

 placement_date                   Date                     Yes      Monitoring trigger.

 condition / status / photo       Select/Attach                No   For reconciliation.

Sample Display Reconciliation (transactional)
Reconciles sample/display stock held at dealer locations.

 Field                            Type                  Req.        Notes
 recon_no / recon_date            Data/Date                Yes      Reference and cycle date.

                                  Link (Display
 placement_slip                                            Yes      Source display.
                                  Placement Slip)

 dealer_location / item           Link                     Yes      Where and what.

 response                         Select                   Yes      Still displayed / Removed / Loose-not-shown.

 responded_via /
                                  Select/Link                  No   WhatsApp button / staff.
 responded_by

 action                           Select                       No   Keep / Put-up replacement / Pullback.

 remarks                          Small Text                   No   Notes.

Override Approval (transactional (log))
Backs the submit-freeze approval workflow.

 Field                            Type                  Req.        Notes
 reference_doctype /
                                  Dynamic Link             Yes      Document under approval.
 reference_name

 override_type                    Select                   Yes      Credit limit / Overdue / Price / Discount / Amend / Cancel.

 old_value / new_value            Data                         No   Version diff.

 requested_by / approver /
                                  Link/Select              Yes      Parties and outcome.
 status

 channels_notified                Select/Data                  No   WhatsApp / Email / App / SMS.

Vehicle Tracking Log (transactional (+ Ping/Stop child))
Holds live location pings and stop events for a pickup plan.

 Field                            Type                  Req.        Notes
                                  Link (Pickup /
 pickup_plan                                               Yes      Trip being tracked.
                                  Route Plan)

 vehicle_number                   Data                     Yes      Truck on the trip.

                                  Data/Geolocatio
 current_location                                              No   Latest known location.
                                  n

                                  Table (Stop                       Child: supplier, arrival_time, departure_time, dwell_time,
 events                                                        No
                                  Event)                            picked_qty, weight.

 last_updated                     Datetime                     No   Last ping time.

Labour Attendance & Payment (transactional (+ Attendance child))
Tracks labourer attendance and payment; links to unloading where relevant.

 Field                            Type                  Req.        Notes
 labourer                         Data/Link                Yes      Labourer (name / master reference).

 period_from / period_to          Date                         No   Attendance period.

                                  Table
                                                                    Child: date, shift, present, work_type,
 attendance                       (Attendance                  No
                                                                    linked_unloading_voucher.
                                  Line)

 days_present /
 amount_due /                     Float/Currency               No   Computed dues and payment.
 amount_paid

 payment_mode / txn_ref /
                                  Select/Data/Date             No   How and when paid.
 payment_date

 status                           Select                       No   Pending / Partly paid / Paid.

Unallocated Stock Entry (transactional)
Loose stock kept outside an allocated bay (C.6.6).

 Field                            Type                  Req.        Notes
 item / batch / qty               Link/Float               Yes      What and how much.

 ad_hoc_location                  Data                     Yes      Where it was placed (scanned/space-tag).

 recorded_by /
                                  Link/Datetime                No   Audit.
 recorded_on

 action                           Select                       No   Pending / Allocated to bay / Merged.

 bay_allocation_slip              Link                         No   On allocation.

### D.4 Item Child Tables (Custom)
Product Image Gallery (child of Item)
Image set per item (used by catalog, dealer app, WhatsApp).

 Field                            Type                  Req.        Notes
 image                            Attach Image             Yes      Image file.

 image_type                       Select                   Yes      Product / Application (room) / Single Tile / Live / Additional.

 is_primary                       Check                        No   Primary display image.

 source / uploaded_by /           Data/Link/Dateti                  Live images only from authorised registered mobile
                                                               No
 uploaded_on                      me                                numbers; bulk images from Google Drive.

Customer Part Number (Dealer Code) (child of Item)
Dealer-specific code mapping; enables search by dealer's own code.

 Field                            Type                  Req.        Notes
 dealer                           Link (Customer)          Yes      Dealer.

                                                                    Dealer's code (reverse-mapped on inquiry / app /
 customer_item_code               Data                     Yes
                                                                    WhatsApp).

 sample_issued                    Check                        No   Gates catalog visibility for the dealer.

 remarks                          Data                         No   Notes.

### D.5 Custom Workflows, Automations & Print Formats
The following are custom (server scripts, scheduled jobs, workflows, portal/print components) required
beyond ERPNext defaults.

 Custom component                               Purpose
 Series → Item propagation                      Copy series attributes, price and bulk/retail thresholds to new items.
                                                Resolve dealer code / company code / image to internal item within
 Code resolver service
                                                issued-sample scope.
 Duplicate inquiry / PO detection               Flag repeats within the configurable window.(Timeline - 48hrs)

 Custom component                               Purpose
 Missed-demand aggregation                      Aggregate unmet inquiries into reorder suggestions.
 Reorder auto-calc (scheduled)                  Generate the review-only Reorder Plan from retail sales and stock.
 Bay-allocation algorithm                       Suggest bay by category/batch/size/occupancy/buffer; produce slip.
 QR / sticker print + batch print               Box and sample sticker formats; per-box print run.
 Warehouse scanning validation                  Inward/pick/dispatch scan-validate-confirm mobile UI.
 Dispatch payment lock                          Block dispatch until advance confirmed via VALS API.
 Supplier-delay impact check                    Compare supplier vs customer timeline, escalate/notify.
 Sample placement monitoring
                                                Auto-create placement slip, reminders, reconciliation.
 (scheduled)
 Product withdrawal batch job (daily)           Active→PDC→Against Order→Discontinued transitions and triggers.
 WhatsApp flow engine                           Menu/code/image resolution, templates, buttons, reminders (dealer & staff).
 Dealer app portal                              Dealer-scoped catalog, enquiry-to-order, status/dues/history.
 Catalog & price-list formats                   Dealer-scoped, image-rich, price-inclusive/exclusive.
 Image bulk import                              Google Drive filename→item-code matching into image gallery.
 OCR customer-PO ingestion                      Extract PO/items/qty; confirm-on-mismatch.
 e-Way bill automation                          Pin-code distance → auto-generate for interstate/threshold.
 Approval routing & notifications               Six override triggers, amend/cancel version diff, multi-channel.
 Inquiry price derivation                       Select price list by dealer classification + purchase history at quote time.
 Batch-combination suggestion                   Suggest single batch, else a 2–3 batch combination meeting the required qty.
 Vehicle tracking + stop capture                Live location, per-stop pickup and dwell-time capture; driver route sharing.
 Loose / unallocated stock tracking             Scan-and-record loose stock; unallocated report; allocate/merge flow.
 Reorder review notification                    Notify responsible members to review the auto-generated draft plan.
 Labour attendance & payment                    Track labourer attendance and dues/payments.

## Part E — Integrations
 Integration                                    Technical Notes
                                                Business messaging with controlled menus, approved templates and
 WhatsApp / Meta                                response buttons; reminders to dealers and staff. Depends on Meta
                                                approvals.
                                                ERPNext-integrated bank transfer and real-time transaction reflection,
                                                enabling advance verification and dispatch release. Only available India
 India Banking (VALS API)
                                                Banking Application functionality is provided; automatic payout bank charges
                                                out of scope.
                                                GST-compliant invoicing and automatic e-Way bill generation from pin-code
 GST & e-Way Bill API
                                                distance.
                                                Supplier GPS capture and route optimisation by time and by distance (both
 Maps / Routing API
                                                options). Real-time traffic and toll preference not decision factors.
                                                Extraction of PO number, items and quantities; confirm-on-mismatch.
 OCR / AI (customer PO)
                                                Supplier document reading subject to feasibility.
                                                Bulk image import; filenames follow an item-code + image-type convention
 Google Drive
                                                for matching.
 AI Image Search & Recommendation               Wave 2. Accuracy depends on image quality and tagging.

## Part F — Implementation Approach
Phase 1 is delivered in two waves and structured around four milestones. An operational go-live can
follow Wave 1, with Wave 2 assisted-intelligence features continuing under Phase 1.

                                           Figure 16 — Phased implementation roadmap

### F.1 Milestones (Scope)
 Milestone                                      Scope Covered
                                                Project confirmation, kickoff, solution design, master templates, user/role
 Milestone 1
                                                planning, Tally data review.
                                                Core system setup, master migration, dealer/item/supplier setup, inquiry
 Milestone 2
                                                workflow, pricing, purchase reorder planning.
                                                Warehouse bay allocation, QR/scanning, purchase pickup planning,
 Milestone 3                                    damage/insurance workflow, unloading payment tracking, WhatsApp dealer
                                                flow, dealer app.
                                                Assisted AI image search, product recommendation, route/truck planning,
 Milestone 4
                                                forecasting, dashboards, UAT, training and go-live.

### F.2 Delivery Waves

Wave 1 — Operational Backbone
Core system setup; Tally data migration; masters (Series, Item, Dealer, Supplier, Transporter, Warehouse
& Bay); dealer inquiry workflow; product catalog and images; pricing; purchase reorder planning;
purchase pickup planning; warehouse bay management and mobile scanning; QR/sticker printing;
damage and insurance workflow; unloading payment tracking; web-based dealer app; WhatsApp dealer
interaction; reports and dashboards; UAT and operational go-live.

Wave 2 — Assisted Intelligence & Optimisation
Assisted AI image search; assisted AI product recommendation; assisted route planning and
truck-capacity planning; assisted forecasting; stock-clearance suggestions; display-replacement
intelligence.

### F.3 Data Migration
Source system is Tally Prime Edition 7.1. Scope covers the current year plus one prior year, subject to
data availability and a clean exportable format. A sample export is validated first, then masters and
transactions are mapped, cleaned, validated and imported.

                                                Figure 17 — Tally data migration flow

### F.3.1 Migration Scope

   •​ Masters: dealer/customer, supplier, item/product (with series mapping).
   •​ Opening stock and opening balances.
   •​ Outstanding receivables and payables.
   •​ Historical sales and purchase transactions for the current year plus one prior year, where available
      in clean export format.
### F.3.2 Key Migration Steps
   1.​ Validate a sample export first to confirm structure and field availability.
   2.​ Normalise UOM: convert Tally's “box of N pieces” units to a single Box UOM plus a conversion
       factor; carry weight and area conversions.
   3.​ Map dealer codes to company item codes (bulk templates for key dealers) so both codes resolve
       post-migration.
   4.​ Map items to the new Series structure where possible.
   5.​ Clean, de-duplicate and reconcile masters and balances before final import.
   6.​ Import masters, then opening stock/balances/outstandings, then transactions; reconcile and sign
       off.
### F.3.3 Customised Tally Fields to Migrate
Pacific Inc's Tally is customised; the following custom/derived fields must be captured and mapped (not
just standard Tally masters):

 Tally custom / derived data                    Target in ERPNext
 Customised unit “box of N pieces” and          Item UOM = Box + conversion factor (pieces/box); Sq Ft and Sq Metre
 conversion                                     conversions.
 Item attributes embedded in item
 name/series (size, thickness, finish,          Item attribute fields + Series master.
 series)
 Retail vs bulk / display / sample
                                                Series bulk/retail thresholds + invoice classification tag.
 classification
 Reorder screen parameters (avg sales
 basis, reorder frequency, minimum              Reorder Plan configuration and item reorder frequency.
 levels)
 Dealer price classification and three
                                                Customer classification + three ERPNext Price Lists.
 price lists
 Batch with date of manufacturing and
                                                Batch + custom batch weight field.
 variable weight
 Damage / breakage records captured
                                                Damage/claim references (opening position).
 at sale or purchase
 Dealer-specific product codes                  Item Customer Part Number child table.

  Migration dependency

 Tally custom / derived data                    Target in ERPNext

  Depth and completeness of
  historical migration depend on the
  cleanliness and exportability of the
  Tally data. Cleaning beyond the
  agreed templates is handled
  separately.

### F.4 ERPNext Customisation Plan
The build is delivered as an ERPNext app for Pacific Inc containing the custom doctypes, fields,
workflows, scripts, jobs, print formats, portal pages and integrations. Summary of what is configured vs
custom-developed:

 Layer                                          What is built
                                                Series, Transporter (+Vehicle), Warehouse Bay, Dealer Inquiry (+Item), Bay
                                                Allocation Slip, Reorder Plan (+Row), Pickup/Route Plan (+Stop), Vehicle
                                                Tracking Log, Claim Voucher (+Item), Unloading Payment Voucher, Labour
 Custom doctypes
                                                Attendance & Payment, Sample Request, Display Placement Slip, Sample
                                                Display Reconciliation, Unallocated Stock Entry, Override Approval; Item child
                                                tables (Image Gallery, Customer Part Number).
                                                Supplier GPS; Item series link, colour/finish/status, batch weight; Customer
 Custom fields on native doctypes
                                                classification/type/scope; transaction weight display and drop-ship flags.
                                                Reorder auto-calc; product-withdrawal daily job; sample placement
 Scheduled jobs
                                                monitoring/reminders; supplier-delay impact check.
                                                Series→Item propagation; code resolver; duplicate detection; bay-allocation
 Server / client scripts                        suggestion; dispatch payment lock; e-Way bill trigger; price working
                                                calculations.
                                                Submit-freeze with six override triggers; amend/cancel version-diff routing;
 Workflows
                                                approvals with mobile + multi-channel notification.
                                                Box/inward sticker, dealer sample sticker, dealer catalog (image-rich,
 Print formats                                  price-inclusive/exclusive), dealer price list, key transaction formats with
                                                weight.
                                                Web dealer app (portal); WhatsApp flow engine + middleware; warehouse
 Portal & channels
                                                mobile scanning UI.
                                                India Banking (VALS API), GST/e-Way, Maps/Routing, OCR/AI, Google Drive
 Integrations
                                                image import, WhatsApp/Meta.
 Reports & dashboards                           The report set in C.12.2 and role dashboards; Wave 2 assisted-AI features.

### F.5 Implementation Steps
Indicative sequence; Wave 1 modules are built and rolled out first, with Wave 2 following.

                                                  Figure 18 — Implementation steps

   7.​ Environment & instance setup: cloud hosting, ERPNext instance, custom app skeleton, middleware
       and dealer-app scaffolding.
   8.​ Masters & configuration: Series, Item, Dealer, Supplier (GPS), Transporter & Vehicle, Warehouse &
       Bay, price lists, UOM/weight conversions, roles and permissions, payment terms.
   9.​ Tally data migration: sample validation, UOM normalisation, dealer-code mapping, custom-field
       mapping, clean/validate/import (current + last year).
   10.​Custom doctypes & workflows: inquiry/CRM, reorder plan, bay allocation, claim voucher,
       unloading, sample/display, approvals.
   11.​Channel build: WhatsApp flow (templates, buttons, reminders), web dealer app enquiry module,
       warehouse mobile scanning.
   12.​Integrations: India Banking (VALS API), GST/e-Way bill, Maps/Routing, OCR customer-PO, Google
       Drive image import.
   13.​Print formats and reports/dashboards: stickers, catalog, price list; report set and role dashboards.
   14.​UAT, training and a full mock run of the end-to-end flows.
   15.​Wave 1 operational go-live.
   16.​Wave 2: assisted AI image search and recommendation, route/truck optimisation, forecasting.

### F.6 Timeline & Dependencies

  Indicative timeline: approximately 6 to 7 months
  Depends on: final scope confirmation, Tally data quality, product-image availability, WhatsApp/Meta approval
  timelines, PDA/scanner hardware testing, user availability for testing, and timely approvals from Pacific Inc.

### F.7 Assumptions & Exclusions (Technical)
 Assumptions                                    Exclusions
 ~10 internal users for initial sizing (no
                                                Native iOS/Android app; offline mobile app.
 hard cap).
 ~150 dealers via dealer app and
                                                Hardware procurement and warranty (PDA/scanners billed separately).
 WhatsApp.
 Current + one prior year Tally
                                                Product photography and manual image cleanup.
 migration.
 Product images provided
 item-code-wise by Pacific/vendors, or          Data cleaning beyond agreed templates; migration beyond current + one
 via Google Drive with the naming               year.
 convention.
 WhatsApp depends on Meta
                                                Enterprise-grade AI accuracy guarantee; marketplace/e-commerce
 approvals; AI is assisted and
                                                integration.
 image-quality dependent.
 Route/truck planning is assisted, not a
                                                Direct banking integration other than the India Banking Application.
 fully automated logistics engine.

### F.8 Success Factors
   •​ Timely confirmation of scope and workflow approvals.
   •​ Clean Tally data export and availability of product images (with the agreed naming convention).
   •​ Clear internal process owners from Pacific Inc.
   •​ Timely user testing and dedicated coordination during UAT.
   •​ Hardware finalisation for PDA/scanning.
   •​ Timely WhatsApp and template approvals.

### F.9 Information Required from Client (Dependencies)
The following are needed from Pacific Inc to configure and build accurately. They are dependencies on
the client and should be collected during Milestone 1.

 Item required from client                      Used for
 GST and tax structure for items (HSN,
                                                Item tax setup, GST invoicing and e-Way bill.
 GST rates, any tax categories).
 Current series numbering scheme and
                                                Series master design, item coding and print-format replication.
 the print formats presently used.
 User list with their roles and access
                                                Role and permission configuration.
 levels.

 Item required from client                      Used for
 Bay size and bay-type list, and the
 warehouse design / layout and                  Warehouse Bay master and bay-allocation logic (C.1.8 / C.6.3).
 capacity.
 Dealer classification measurement
 basis (confirm period / rolling window         Dealer classification automation (C.1.4).
 for the sales bands).
 Product images (item-code-wise) or
 Google Drive folder with the naming            Catalog, dealer app, WhatsApp and image bulk-import.
 convention.
 Tally export (current + prior year)
                                                Data migration (F.3).
 including customised fields.

## Part G — Open Points & Configurable Values
The following were discussed as open or configurable and should be finalised during solution design.
They are treated as configurable parameters; indicative values are starting points only.

 Open Point                                     To Confirm / Configure
                                                Confirmed (configurable): <₹1L Standard, ≥₹1L Dealer, ≥₹15L Master Dealer;
 Dealer classification bands
                                                confirm the measurement period / rolling window.
 Insurance %                                    Configurable per series.
 Bulk / retail thresholds                       Per-series bulk and retail quantity thresholds (by size).
 Duplicate-inquiry window                       Configurable (48 hours starting default).
 Missed-demand matching                         Configurable quantity-variation threshold and match rules.
                                                Company / vendor / production MOQ and applicability of the indicative
 MOQ hierarchy
                                                80-box figure.
 Stock reservation review/release               Holding period before review; automatic vs approval-based release.
 Supplier-delay escalation                      Delay threshold that triggers customer-impact escalation.
                                                Advance percentages and dispatch-blocking conditions per dealer/item (via
 Advance-payment rules
                                                Payment Terms).
 Dealer-facing batch visibility                 Top-3 batch rule and single-batch handling.
                                                Monitoring cycle and reminder intervals; store-count criterion for
 Sample/display cycle
                                                withdrawal.
 Product-withdrawal thresholds                  Series-level annual sales threshold and display-duration / store-count criteria.
 Route optimisation default                     Both time and distance provided; confirm default preference.
                                                Supported formats, minimum quality, confidence threshold and
 OCR / image matching
                                                manual-verification flow; image filename convention.
 Salesperson assignment                         Salesperson-to-dealer assignment, targets and incentive rules.

                                                       — End of Document —

