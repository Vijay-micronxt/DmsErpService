"""Quotation and Sales Order are ERPNext's own native doctypes — no custom doctype
needed for either. Only the handful of Pacific-specific attributes they have no
equivalent for become Custom Fields:

- Quotation: the markup % applied over the approved dealer price (BRD §7.6 retail
  markup), and freight (kept as a plain field rather than wired into Sales Taxes and
  Charges, since that needs a GL account this app can't assume exists on every site).
- Sales Order: a warehouse-fulfillment stage (Confirmed -> Picking -> Ready to
  Dispatch -> Dispatched -> Delivered/Cancelled) that's a distinct, manually-advanced
  operational flow layered on top of — not derived from — ERPNext's own
  delivery/billing status, plus a structured stage-history log (no equivalent to a
  queryable, typed timeline in core Frappe) and a vehicle/source-document reference.
- Both: `custom_order_channel` (Phase 15, BRD "Retail vs bulk report"). Before this,
  every Quotation/Order in the app was *implicitly* retail — the reorder engine's own
  docstring already claimed "retail/sub-dealer channel only (bulk/project orders
  excluded per BRD §12.1)", but nothing actually tagged an order as bulk, so nothing
  was really being excluded. This field is what makes that claim true, and is what a
  Retail vs Bulk report groups by. Defaults to Retail, matching every order created
  before this field existed.
- Quotation Item: `custom_delivery_date`, a per-line delivery date (BRD C.3.1 --
  "delivery date... maintained item-wise"). Unlike Sales Order Item, ERPNext's own
  Quotation Item has no native `delivery_date` field to reuse -- quotation_api.py
  used to read/write `row.delivery_date` on Quotation Item rows anyway, which raised
  a genuine AttributeError the moment a document was reloaded from the database
  (frappe.get_doc's freshly-loaded child rows only carry attributes for real,
  meta-defined fields) -- e.g. every `update_quotation_line_qty` call, since that
  reloads the doc before touching any line.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from dms_erp.catalog.utils import ORDER_UOMS
from dms_erp.pricing.dealer_classification import DEALER_CLASSIFICATION_STANDARD, DEALER_CLASSIFICATIONS
from dms_erp.sales.order_channel import DEALER_TYPES

ORDER_STAGES = ["Confirmed", "Picking", "Ready to Dispatch", "Dispatched", "Delivered", "Cancelled"]
ORDER_EVENT_STAGES = ["Created"] + ORDER_STAGES
ORDER_SOURCE_TYPES = ["Inquiry", "Quotation", "Direct"]
ORDER_CHANNELS = ["Retail", "Bulk", "Project"]

CUSTOM_FIELDS = {
	"Quotation": [
		{
			"fieldname": "dms_quotation_section",
			"fieldtype": "Section Break",
			"label": "DMS Pricing",
			"insert_after": "valid_till",
		},
		{"fieldname": "custom_markup_pct", "fieldtype": "Percent", "label": "Retail Markup %", "insert_after": "dms_quotation_section"},
		{"fieldname": "custom_freight", "fieldtype": "Currency", "label": "Freight", "insert_after": "custom_markup_pct"},
		{
			"fieldname": "custom_delivery_pincode",
			"fieldtype": "Data",
			"label": "Delivery Pincode",
			"description": "ISS-2026-00633 -- free-text; quotation_api.suggest_transport_rate finds every Transport Rate Zone whose pincode range covers this for a freight SUGGESTION only, never an enforced rate.",
			"insert_after": "custom_freight",
		},
		{"fieldname": "custom_inquiry", "fieldtype": "Link", "label": "Source Inquiry", "options": "Inquiry", "insert_after": "custom_delivery_pincode"},
		{
			"fieldname": "custom_order_channel",
			"fieldtype": "Select",
			"label": "Channel",
			"options": "\n".join(ORDER_CHANNELS),
			"default": "Retail",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"insert_after": "custom_inquiry",
		},
	],
	"Sales Order": [
		{
			"fieldname": "dms_fulfillment_section",
			"fieldtype": "Section Break",
			"label": "DMS Fulfillment",
			"insert_after": "delivery_date",
		},
		{
			"fieldname": "custom_fulfillment_stage",
			"fieldtype": "Select",
			"label": "Fulfillment Stage",
			"options": "\n".join(ORDER_STAGES),
			"default": "Confirmed",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"insert_after": "dms_fulfillment_section",
			"allow_on_submit": 1,
			# Moved only through order_api.advance_order_stage, which applies the cancel
			# approval (BRD C.11) and the advance gate (C.3.4); editable in Desk it skipped both.
			"read_only": 1,
		},
		{
			"fieldname": "custom_vehicle",
			"fieldtype": "Data",
			"label": "Vehicle",
			"description": "ISS-2026-00630 -- primary search key for outward shipments (BRD §C.12: lorry number).",
			"search_index": 1,
			# A Sales Order is submitted (docstatus=1) by the time its vehicle is actually
			# known -- without this, Frappe's UpdateAfterSubmitError blocks setting it at
			# dispatch time at all (confirmed directly: advance_order_stage's new vehicle=
			# param threw exactly this before the flag was added).
			"allow_on_submit": 1,
			"insert_after": "custom_fulfillment_stage",
		},
		{
			"fieldname": "custom_driver_mobile",
			"fieldtype": "Data",
			"label": "Driver Mobile",
			"description": "ISS-2026-00630 -- alternate primary search key for outward shipments, alongside Vehicle.",
			"search_index": 1,
			"allow_on_submit": 1,
			"insert_after": "custom_vehicle",
		},
		{"fieldname": "dms_fulfillment_column_break", "fieldtype": "Column Break", "insert_after": "custom_driver_mobile"},
		{"fieldname": "custom_source_type", "fieldtype": "Select", "label": "Source Type", "options": "\n".join(ORDER_SOURCE_TYPES), "insert_after": "dms_fulfillment_column_break"},
		{"fieldname": "custom_source_ref", "fieldtype": "Data", "label": "Source Reference", "insert_after": "custom_source_type"},
		{
			"fieldname": "custom_stage_history",
			"fieldtype": "Table",
			"label": "Stage History",
			"options": "Order Stage Event",
			"insert_after": "custom_source_ref",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "custom_order_channel",
			"fieldtype": "Select",
			"label": "Channel",
			"options": "\n".join(ORDER_CHANNELS),
			"default": "Retail",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"insert_after": "custom_stage_history",
		},
		{
			"fieldname": "custom_advance_confirmed",
			"fieldtype": "Check",
			"label": "Advance Payment Confirmed",
			"default": "0",
			# Set only by order_api.confirm_advance_payment (Management only); editable in
			# Desk it let anyone open the dispatch gate (BRD C.3.4).
			"read_only": 1,
			"description": "BRD C.3.4 — interim manual gate ahead of the real VALS API advance-payment integration (blocked on external credentials, not built). Set by Management on a per-order basis, no dealer-level default; order_api.advance_order_stage refuses Ready to Dispatch until this is set.",
			"insert_after": "custom_order_channel",
		},
		# ISS-2026-00635 -- set once each by sales.reservation.review_reservations
		# (daily); see that module for the 21-day window. Read-only: they record what
		# the job did, nobody edits them by hand.
		{
			"fieldname": "custom_reservation_reminder_sent_on",
			"fieldtype": "Date",
			"label": "Reservation Reminder Sent On",
			"read_only": 1,
			"allow_on_submit": 1,
			"insert_after": "custom_advance_confirmed",
		},
		{
			"fieldname": "custom_reservation_flagged_on",
			"fieldtype": "Date",
			"label": "Reservation Flagged for Review On",
			"read_only": 1,
			"allow_on_submit": 1,
			"in_standard_filter": 1,
			"insert_after": "custom_reservation_reminder_sent_on",
		},
	],
	"Sales Order Item": [
		# BRD C.3.2 -- set once by sales.delivery_risk.review_delivery_risk (daily) when
		# a delayed supplier PO line threatens this specific line's own delivery_date.
		# Per-line, not header like custom_reservation_flagged_on above, because
		# delivery_date and delay risk both vary per line on a multi-item order.
		# Read-only, never cleared, so the same at-risk line isn't re-notified every day
		# it stays open.
		{
			"fieldname": "custom_delivery_risk_flagged_on",
			"fieldtype": "Date",
			"label": "Delivery Risk Flagged On",
			"read_only": 1,
			"allow_on_submit": 1,
			"insert_after": "delivery_date",
		},
		# Sheet 7.8 -- the qty/unit the line was actually entered in, when it wasn't
		# Box (see catalog.utils.normalize_order_line_uoms). `qty` itself always stays
		# in boxes; these are display-only, so a dealer/staff screen can show "50 Sqft
		# (~4 boxes)" instead of just the converted box count.
		{
			"fieldname": "custom_entered_qty",
			"fieldtype": "Float",
			"label": "Entered Qty",
			"insert_after": "custom_delivery_risk_flagged_on",
		},
		{
			"fieldname": "custom_entered_uom",
			"fieldtype": "Select",
			"label": "Entered UOM",
			"options": "\n".join(ORDER_UOMS),
			"insert_after": "custom_entered_qty",
		},
		# ISS-2026-00678 -- carried from Inquiry.damaged_stock (quotation_api._priced_items),
		# same fieldname as Quotation Item's own below so ERPNext's native Quotation->Sales
		# Order mapper copies it across with no extra glue code (see quotation_api.
		# _convert_to_order's comment on custom_delivery_date for the one field that DOES
		# need that, precisely because its name differs across the two doctypes -- this one
		# doesn't). Read by picking_api.ensure_pick_tasks to route the line to a Damages Bay.
		{
			"fieldname": "custom_damaged_stock",
			"fieldtype": "Check",
			"label": "Damaged Stock",
			"default": "0",
			"insert_after": "custom_entered_uom",
		},
	],
	"Sales Invoice": [
		{
			"fieldname": "custom_order_channel",
			"fieldtype": "Select",
			"label": "Channel",
			"options": "\n".join(ORDER_CHANNELS),
			"default": "Retail",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"insert_after": "due_date",
			"description": "BRD C.4.3 -- carried over from the Sales Order at create_sales_invoice time, so the Retail vs Bulk split (C.4.1's reorder exclusion included) still holds once an order is billed. Native Tally-migrated invoices never get this field set (nothing here rewrites synced records), which is exactly why sample_api.top_dealers and reorder_api/forecasting.py default a missing value to Retail rather than excluding it.",
		},
		{
			"fieldname": "custom_clubbed_orders",
			"fieldtype": "Table",
			"label": "Clubbed Orders",
			"options": "Invoice Clubbed Order",
			"insert_after": "custom_order_channel",
			"description": "Client request (Oct 2026, Vijay/Karthik) -- order_api.create_consolidated_sales_invoice bills several Sales Orders on one invoice. The invoice's own customer_address/shipping_address_name can only hold one value (the first clubbed order's), so every clubbed order's own billing/shipping address is recorded here instead of being silently dropped when they differ. Empty for an invoice raised the ordinary one-order-at-a-time way.",
		},
	],
	"Quotation Item": [
		{
			"fieldname": "custom_delivery_date",
			"fieldtype": "Date",
			"label": "Delivery Date",
			"insert_after": "discount_percentage",
		},
		{
			"fieldname": "custom_entered_qty",
			"fieldtype": "Float",
			"label": "Entered Qty",
			"insert_after": "custom_delivery_date",
		},
		{
			"fieldname": "custom_entered_uom",
			"fieldtype": "Select",
			"label": "Entered UOM",
			"options": "\n".join(ORDER_UOMS),
			"insert_after": "custom_entered_qty",
		},
		# ISS-2026-00678 -- see Sales Order Item's own custom_damaged_stock above; same
		# fieldname on both so ERPNext's native mapper carries it to the Sales Order line.
		{
			"fieldname": "custom_damaged_stock",
			"fieldtype": "Check",
			"label": "Damaged Stock",
			"default": "0",
			"insert_after": "custom_entered_uom",
		},
	],
	"Opportunity": [
		{
			"fieldname": "custom_dms_inquiry",
			"fieldtype": "Link",
			"label": "Source Inquiry (DMS)",
			"options": "Inquiry",
			"read_only": 1,
			"insert_after": "customer_name",
			"description": "sales.inquiry_api._mirror_to_opportunity — set once, at creation, when this Opportunity was auto-created as a one-way visibility mirror of a dms_erp Inquiry. Never updated afterwards; the Inquiry's own status lifecycle (Quoted/Converted/Rejected/etc.) is not pushed back here.",
		},
	],
	"Customer": [
		{
			"fieldname": "dms_classification_section",
			"fieldtype": "Section Break",
			"label": "DMS Dealer Classification",
			"insert_after": "customer_details",
		},
		{
			"fieldname": "custom_dealer_classification",
			"fieldtype": "Select",
			"label": "Dealer Classification",
			"options": "\n".join(DEALER_CLASSIFICATIONS),
			"default": DEALER_CLASSIFICATION_STANDARD,
			"in_list_view": 1,
			"in_standard_filter": 1,
			"description": "BRD C.1.4 — sales-volume price tier, recomputed nightly by recompute_dealer_classifications(). Manual edits are overwritten on the next run.",
			"insert_after": "dms_classification_section",
		},
		{
			"fieldname": "custom_dealer_type",
			"fieldtype": "Select",
			"label": "Dealer Type",
			"options": "\n".join(DEALER_TYPES),
			"default": "Retail",
			"description": "BRD C.4.3 — drives the default order channel for this dealer's quotations/orders when the caller doesn't pass one explicitly. Manually set, never recomputed.",
			"insert_after": "custom_dealer_classification",
		},
		{
			"fieldname": "custom_salesperson",
			"fieldtype": "Link",
			"label": "Salesperson",
			"options": "User",
			"description": "BRD C.12.3 — ownership of the dealer relationship. Targets/performance-vs-target reporting (26 Oct 2026) is sales.sales_target_api, with its own DMS Sales Target doctype.",
			"insert_after": "custom_dealer_type",
		},
		{
			"fieldname": "custom_phone",
			"fieldtype": "Data",
			"label": "Phone",
			"description": "BRD C.13 — the dealer portal's login identifier and OTP delivery target (see auth.dealer_api). Not used elsewhere in the app.",
			"insert_after": "custom_salesperson",
		},
		# Client requirement 8.9 -- several people at one dealer enquire from their own
		# phones. Every number here works for OTP login and WhatsApp exactly like
		# custom_phone (phone_utils.dealer_for_phone checks both).
		{
			"fieldname": "custom_additional_phones",
			"fieldtype": "Table",
			"label": "Additional Phone Numbers",
			"options": "Dealer Phone",
			"insert_after": "custom_phone",
		},
		{
			"fieldname": "custom_price_visible",
			"fieldtype": "Check",
			"label": "Show Price in Dealer Portal",
			"default": "1",
			"description": "BRD C.13.1 — \"price (if enabled for that dealer)\". Gates price/dealerPrice in the dealer portal's own catalog and item-detail responses (sales.dealer_portal_api) only -- staff-facing screens always show price regardless of this flag.",
			"insert_after": "custom_phone",
		},
		{
			"fieldname": "custom_email",
			"fieldtype": "Data",
			"options": "Email",
			"label": "Email",
			"description": "BRD C.13 — WhatsApp + email follow-up (BRD C.2.6/C.10.2), and the dealer portal's second login identifier (auth.dealer_api.login_with_password) once a dealer sets their own password. Must stay unique across dealers once it's used as a login key — see sales.dealer_api._assert_email_available.",
			"insert_after": "custom_price_visible",
		},
		{
			"fieldname": "custom_out_of_station",
			"fieldtype": "Check",
			"label": "Out of Station",
			"default": "0",
			"description": "BRD C.1.4 — a dealer far enough from the warehouse that transport cost justifies at least Master Dealer pricing. Staff-set (no geocoding exists anywhere in this app); read by pricing.dealer_classification.recompute_dealer_classifications as a floor on top of the volume-based tier, never a client-specific location check — see that module for why.",
			"insert_after": "custom_email",
		},
		{
			"fieldname": "custom_gstin",
			"fieldtype": "Data",
			"label": "GSTIN",
			"description": "Dealer's GST registration number. Free text, not validated against the GSTIN checksum format.",
			"insert_after": "custom_out_of_station",
		},
		{
			"fieldname": "custom_address",
			"fieldtype": "Small Text",
			"label": "Address",
			"description": "Dealer's business address.",
			"insert_after": "custom_gstin",
		},
		{
			"fieldname": "custom_last_activity_on",
			"fieldtype": "Date",
			"label": "Last Activity On",
			"read_only": 1,
			"description": "MOM §3.7 -- latest Sales Order, Sales Invoice (incl. Tally history), Inquiry or inbound WhatsApp message for this dealer. Recomputed daily by sales.dealer_activity.flag_inactive_dealers; never hand-edited.",
			"insert_after": "custom_address",
		},
		{
			"fieldname": "custom_inactive",
			"fieldtype": "Check",
			"label": "Inactive",
			"default": "0",
			"read_only": 1,
			"in_list_view": 1,
			"in_standard_filter": 1,
			"description": "MOM §3.7 -- no activity in DMS Sales Settings.dealer_inactivity_days (90 by default). A flag only: nothing is blocked or disabled by it, and it clears itself the next day the dealer is active again.",
			"insert_after": "custom_last_activity_on",
		},
	],
}


# ERP -> Tally sync (see sales/tally_sync.py). Everything but the opt-in tick is written
# only by the sync itself, hence read-only + allow_on_submit. NOT created on migrate
# while the sync is disabled (see hooks.py) -- deliberately kept out of CUSTOM_FIELDS so
# a live site gets no Tally fields at all until it's switched on.
TALLY_SYNC_CUSTOM_FIELDS = {
	"Sales Invoice": [
		{
			"fieldname": "sync_to_tally",
			"fieldtype": "Check",
			"label": "Sync to Tally",
			"default": "0",
			"allow_on_submit": 1,
			"description": "Send this invoice to Tally as a Sales voucher once it is submitted (re-tick after a failure to retry).",
			"insert_after": "due_date",
		},
		{
			"fieldname": "tally_sync_status",
			"fieldtype": "Select",
			"label": "Tally Sync Status",
			"options": "\nPending\nSynced\nFailed\nSkipped",
			"read_only": 1,
			"allow_on_submit": 1,
			"in_standard_filter": 1,
			"no_copy": 1,
			"depends_on": "eval:doc.sync_to_tally || doc.tally_sync_status",
			"insert_after": "sync_to_tally",
		},
		{
			"fieldname": "tally_synced_voucher_no",
			"fieldtype": "Data",
			"label": "Tally Voucher (synced)",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"depends_on": "eval:doc.tally_synced_voucher_no",
			"insert_after": "tally_sync_status",
		},
		{
			"fieldname": "tally_synced_on",
			"fieldtype": "Datetime",
			"label": "Synced to Tally On",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"depends_on": "eval:doc.tally_synced_on",
			"insert_after": "tally_synced_voucher_no",
		},
		{
			"fieldname": "tally_sync_error",
			"fieldtype": "Small Text",
			"label": "Tally Sync Message",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"depends_on": "eval:doc.tally_sync_error",
			"insert_after": "tally_synced_on",
		},
	],
}


def setup_sales():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
