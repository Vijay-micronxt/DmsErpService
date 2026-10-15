# Sales

Phase 5: Inquiries, Quotations, Orders, Picking. Implemented.

A new module — not part of the module list Phase 0 bootstrapped, since the original
plan didn't allocate one for this phase. Added here, matching the frontend's own
domain boundary (Inquiries/Quotations/Orders/Picking are one connected flow).

- **Inquiry** (`inquiry_api.py`, `doctype/inquiry`) — a genuine custom doctype; no
  ERPNext equivalent for "logged demand, not yet a sales document" with Pacific's
  exact 10-state status lifecycle. Unlike most other frontend `api/*.ts` modules,
  `pacific-tileflow`'s inquiries are still static read-only seed data with no
  create/update wired up client-side — this module builds the real, mutable backend
  the BRD describes; the frontend needs its own follow-up work to call it.
  `create_inquiry` (Phase 14) used to enforce the same catalog gate `create_quotation`
  always has — dealer-assigned visibility and current sellability. ISS-2026-00644
  dropped the assigned-visibility half for inquiries specifically: a dealer can now
  inquire on an item Purchase hasn't assigned to them yet (that's the point of
  "inquire-only" on an unassigned item — see catalog/README.md's
  `browsable_catalog_for`); only ordering it still requires assignment, further down
  the funnel at the Quotation/Order step. A Pulled Back item is still rejected here
  either way. `item` itself is no longer always required (ISS-2026-00636 / MOM 3.6,
  "Custom Style") — a free-text `requested_style` can be given instead, for a design
  that isn't in the item master at all (comms.flow_api's new
  `create_custom_style_inquiry`, the WhatsApp menu's "Custom Style" option, is the
  only caller today). Deliberately not an ERPNext Opportunity and not a placeholder
  Item record — every item-shaped step of `_create_inquiry` (sellability, duplicate
  check, stock-derived status, the Opportunity mirror) is skipped for one of these;
  `dealer_portal_api.convert_to_order` rejects converting one to an order outright,
  since there's no real item to build a Sales Order line from.
- **Dealer portal: enquiry → quotation → order** (`dealer_portal_api.py`, client issues
  of Oct 2026) — a dealer no longer turns an enquiry straight into an order. They
  enquire (several items at once via `raise_inquiries`: one Inquiry per item, all
  sharing `request_group`), staff quote them (the Quotation Builder submits
  immediately, so the quotation is visible to the dealer the moment it exists), and
  the dealer sees it with `list_my_quotations`/`get_my_quotation` (a dealer-safe
  shape — no markup %, channel or purchase cost) and places the order with
  `accept_quotation` (their own PO number + dispatch date; built from the quotation
  exactly as quoted, closing every inquiry it covered). **No price is shown before a
  quotation**: catalog, item detail, enquiries and WhatsApp's item lookup all drop it
  (price depends on delivery and other details), and `Customer.custom_price_visible`
  is no longer read anywhere. The catalog splits into `scope="assigned"` / `"other"`
  for the portal's two sections, and `list_my_invoices`/`get_my_invoice` expose the
  dealer's own Sales Invoices (opening-balance and proforma entries left out). The
  DMS Dealer role has no DocPerm on any ERPNext doctype, so the order-building call
  runs through `_run_as_system` after this module's own checks — `convert_to_order`
  (no longer offered by the portal, kept callable) needed the same, and had been
  failing with a permission error without it. WhatsApp's Place Order now follows the same rule: after the
  quantity and note the bot shows a quotation (`flow_api.create_whatsapp_quotation` — a real
  Quotation at the dealer's tier rate, no markup, GST/freight stated as extra, plus an Inquiry for
  the quantity) and the PO step then confirms *that* quotation (`create_dealer_opportunity` with
  `quotation_id`). A Flow that predates this step never sets `quotation_id` and still takes the
  old direct-order path, so an un-updated live bot keeps working until it's re-imported.
- **Quotation** (`quotation_api.py`) — ERPNext's native `Quotation` doctype,
  submitted immediately on creation (no draft step, same pattern as Phase 4's
  Purchase Order). Custom fields only for the retail markup % and freight (BRD
  §7.6) — freight stays a plain field rather than wired into Sales Taxes and
  Charges, since that needs a GL account this app can't assume exists on every
  site. Every line's rate is computed server-side from the approved dealer price
  (never client-supplied), and every item must be in the dealer's assigned catalog
  and currently sellable — real gates, not just report-time checks (Phase 11 closed
  the sellability half: `is_visible` alone doesn't catch an item pulled back after
  being assigned visible). Line editing (`add_quotation_line`/`remove_quotation_line`/
  `update_quotation_line_qty`, Phase 12) goes through ERPNext's standard cancel +
  amend cycle rather than mutating a submitted document in place — every line's rate
  is rebuilt from the current approved price on every edit, and the quotation's `id`
  changes each time, same as amending any other submitted ERPNext document.
  `update_quotation_status` (Phase 14) only supports setting `Lost` — every other
  status is a side effect of create/amend/`convert_to_order`, not something a
  caller sets directly — via ERPNext's own `declare_enquiry_lost`, the native way
  this doctype supports a manual post-submit status change. `custom_delivery_pincode`
  (ISS-2026-00633, free text) plus `suggest_transport_rate` and the **Transport
  Rate Zone** doctype give the Quotation Builder a freight *suggestion* — never an
  enforced rate. Each zone is a pincode range (from/to), an optional vehicle type
  and a rate Per Trip, Per Kg (× the quote's weight) or Per Km (× a distance stored
  on the zone — no maps/distance API is integrated); ranges may not overlap for the
  same vehicle type. Every zone covering the pincode comes back as an option; none
  covering it returns `None`, not a guess. The master is empty until Pacific's real
  rates are entered, from the staff app's Settings → Transport Rates
  (`transport_rate_api.py`, Management-only writes). Quotation lines also carry
  `belowPurchaseCost` — the ticket's "flag if quoted below standard purchase price",
  compared against the item's Item Price Proposal purchase cost; a warning, never a
  block, and no flag at all when no cost is recorded. MOM §3.1's "6-month average freight
  cost per kg by transport type" is `freight_cost_api.freight_reference`, shown to **staff
  only** beside the Freight field: nothing else records what freight actually cost, so it
  averages `Freight Cost Entry` rows (one per shipment: date, Truck/Container, kg, cost —
  entered under Settings → Freight Costs or bulk-imported from Pacific's history), weighted
  by kg, over the last 6 months. Empty until entries exist; never shown to a dealer (MOM
  §3.2 hides internal cost from the dealer-facing quotation).
- **Order** (`order_api.py`) — ERPNext's native `Sales Order`. Pacific's warehouse-
  fulfillment stages (Confirmed → Picking → Ready to Dispatch → Dispatched →
  Delivered/Cancelled) are a distinct operational flow layered on top via a custom
  field + a structured stage-history child table — advancing them does not itself
  create a Delivery Note or move stock; that's a natural future refinement. An
  order sourced directly from an Inquiry (no markup) and one converted from a
  Quotation (via ERPNext's own native mapper) are the only two creation paths,
  matching the frontend's `sourceType: "Inquiry" | "Quotation"`. `total` (Phase 14)
  is ERPNext's own `grand_total` — always server-computed, since every line's
  `rate` came from `get_dealer_price` at creation, never a client-supplied value.
- **Pick Task** (`picking_api.py`, `doctype/pick_task`) — a genuine custom doctype.
  ERPNext's native Pick List exists but is a coarser, all-or-nothing document per
  pick run; Pacific wants one row per order line with a suggested bay, a partially-
  allocatable qty, a named picker and a simple Pending/Allocated/Picked status,
  which Pick List doesn't model at that granularity. Auto-allocation reads live
  stock straight from `warehouse/utils.py` — nothing here owns its own view of
  what's on hand. Entering the Picking stage on an order auto-creates its tasks.

Sales/Management manage Inquiries/Quotations/Orders; Warehouse/Management manage
Pick Tasks; everyone reads.

- **Dealer directory** (`dealer_api.py`, Phase 9) — every module above already
  treats "dealer" as a bare native `Customer` (`Inquiry.dealer`, `Quotation.
  party_name`, `Sales Order.customer`, `Dealer Catalog.dealer` — none add a custom
  field to `Customer`), but no module ever exposed a list/get read endpoint for it.
  `list_dealers`/`get_dealer` add that: no new doctype or field, just the read
  surface over `Customer` that every other endpoint already assumed existed
  upstream.
- **Inquiry → Purchase Requirement** (`inquiry_api.convert_to_purchase_requirement`,
  Phase 12) — closes the one Inquiry status nothing ever set: "Mapped to PO". It's a
  thin wrapper over `purchase.po_api.create_purchase_order` (which still enforces its
  own Purchase/Management role gate), not a parallel "purchase requirement" doctype —
  a requirement here is just a PO with a `custom_source_inquiry` link back, only
  allowed from an Open/Out of Stock/Pre-order Required inquiry.
- **Order channel** (`custom_order_channel` on Quotation and Sales Order, Phase 15 —
  BRD "Retail vs bulk report") — Retail / Bulk / Project, defaults to Retail. Before
  this field existed, every order in the app was *implicitly* retail — the reorder
  engine's own docstring already claimed "retail channel only, bulk/project excluded
  per BRD §12.1", but nothing tagged an order as bulk, so nothing was really being
  excluded. `create_quotation`/`create_order` both accept `channel`; converting a
  Quotation to an Order carries its channel across; `purchase.reorder_api` now
  actually filters `recentRetailSalesQty` to `channel = 'Retail'`, making that
  docstring's claim true for the first time.
- **Dealer inactivity** (`dealer_activity.py`, MOM §3.7 / action item #6) — daily
  job flags a dealer `custom_inactive` after `DMS Sales Settings.dealer_inactivity_days`
  (90) with no Sales Order, Sales Invoice (incl. Tally history), Inquiry or inbound
  WhatsApp message, and sends one in-app notification when dealers *newly* cross the
  line (to `inactive_dealer_notify_user`, else all DMS Management). Informational
  only — nothing is blocked. `Customer.creation` is only the fallback for a dealer
  with no activity at all, not a floor under real activity: the migration created
  every Customer on one day, and a live check found 265 of 394 dealers dormant.
  Monthly, `notify_active_dealer_numbers` announces the `active_dealer_numbers` list
  (WhatsApp Business compliance); what's done with that list stays outside dms_erp.
- **Reservation timeline** (`reservation.py`, ISS-2026-00635) — daily job over open
  Sales Orders (not Dispatched/Delivered/Cancelled), counted from the order date:
  one reminder at `DMS Sales Settings.reservation_reminder_day` (20) to the dealer's
  salesperson (Management if none), and a review flag once older than
  `reservation_max_days` (21) to Management. Flag only — the ticket rules out
  auto-cancel; releasing the stock stays a person's call. Both dates are stored on
  the order and never cleared, so each happens once; `order_api` returns the state
  as `reservation`.
