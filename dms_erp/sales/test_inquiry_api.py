import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog import dealer_catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.purchase.setup import setup_purchase
from dms_erp.sales import inquiry_api
from dms_erp.sales.utils import DUPLICATE_INQUIRY_WINDOW_DAYS
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_dealer, make_item, make_supplier


class TestInquiryApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_purchase()
		setup_warehouse()
		cls.item = make_item("INQ-TEST-ITEM", "Vitrified")
		cls.dealer = make_dealer("Inquiry Test Dealer")
		cls.supplier = make_supplier("Inquiry Test Supplier")
		cls.bay = make_bay("INQ-TEST-BAY-01", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_inquiry_exposes_created_at_timestamp(self):
		# Inquiry.date is a plain Date field (no time-of-day) -- createdAt is the
		# actual creation Datetime, for tables that need to show time alongside date.
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=5, source="WhatsApp")
		self.assertIsNotNone(inquiry["createdAt"])

	def test_create_inquiry_with_no_stock_is_out_of_stock(self):
		# BRD C.2.4 / Phase 22 -- status is derived from real on-hand qty at creation
		# (previously always hardcoded "Open"), which is what feeds the reorder
		# engine's missed-demand signal (purchase.reorder_api.MISSED_DEMAND_STATUSES).
		item = make_item("INQ-TEST-NO-STOCK", "Vitrified")
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=100, source="WhatsApp")
		# 12 Nov 2026 rebuild: workflow status (parent) and stock availability
		# (per-item) are separate fields now -- status stays "Open" at creation
		# regardless of stock, and the no-stock signal moves to the item row.
		self.assertEqual(inquiry["status"], "Open")
		self.assertEqual(inquiry["items"][0]["availabilityStatus"], "Out of Stock")
		self.assertEqual(inquiry["dealerId"], self.dealer)
		self.assertEqual(inquiry["items"][0]["qty"], 100)
		self.assertIsNone(inquiry["duplicateOf"])

	def test_create_inquiry_fully_covered_by_stock_is_available(self):
		item = make_item("INQ-TEST-FULL-STOCK", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="INQ-TEST-FULL-B1", total_qty=100, lines=[{"bay": self.bay["code"], "qty": 100}], supplier=self.supplier
		)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=40, source="Phone")
		self.assertEqual(inquiry["items"][0]["availabilityStatus"], "Available")

	def test_create_inquiry_partially_covered_by_stock_is_partially_available(self):
		item = make_item("INQ-TEST-PARTIAL-STOCK", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="INQ-TEST-PARTIAL-B1", total_qty=20, lines=[{"bay": self.bay["code"], "qty": 20}], supplier=self.supplier
		)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=40, source="Phone")
		self.assertEqual(inquiry["items"][0]["availabilityStatus"], "Partially Available")

	def test_create_inquiry_flags_a_recent_open_duplicate(self):
		dealer = make_dealer("Inquiry Duplicate Dealer")
		first = inquiry_api.create_inquiry(dealer=dealer, item=self.item, qty=10, source="Phone")
		second = inquiry_api.create_inquiry(dealer=dealer, item=self.item, qty=20, source="WhatsApp")
		self.assertEqual(second["duplicateOf"], first["id"])

	def test_create_inquiry_does_not_flag_a_closed_duplicate(self):
		dealer = make_dealer("Inquiry Closed Duplicate Dealer")
		first = inquiry_api.create_inquiry(dealer=dealer, item=self.item, qty=10, source="Phone")
		inquiry_api.update_inquiry(first["id"], {"status": "Converted to Order"})

		second = inquiry_api.create_inquiry(dealer=dealer, item=self.item, qty=20, source="WhatsApp")
		self.assertIsNone(second["duplicateOf"])

	def test_create_inquiry_does_not_flag_a_duplicate_outside_the_window(self):
		dealer = make_dealer("Inquiry Stale Duplicate Dealer")
		first = inquiry_api.create_inquiry(dealer=dealer, item=self.item, qty=10, source="Phone")
		frappe.db.set_value("Inquiry", first["id"], "date", add_days(today(), -(DUPLICATE_INQUIRY_WINDOW_DAYS + 1)))

		second = inquiry_api.create_inquiry(dealer=dealer, item=self.item, qty=20, source="WhatsApp")
		self.assertIsNone(second["duplicateOf"])

	def test_create_inquiry_does_not_flag_a_different_dealer_or_item(self):
		other_dealer = make_dealer("Inquiry Duplicate Other Dealer")
		other_item = make_item("INQ-DUP-OTHER-ITEM", "Vitrified")
		inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")

		different_dealer = inquiry_api.create_inquiry(dealer=other_dealer, item=self.item, qty=10, source="Phone")
		self.assertIsNone(different_dealer["duplicateOf"])

		different_item = inquiry_api.create_inquiry(dealer=self.dealer, item=other_item, qty=10, source="Phone")
		self.assertIsNone(different_item["duplicateOf"])

	def test_inquiry_carries_the_items_weight(self):
		frappe.db.set_value("Item", self.item, "custom_weight_per_box_kg", 28)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="WhatsApp")
		self.assertEqual(inquiry["items"][0]["weightPerBoxKg"], 28)
		self.assertEqual(inquiry["items"][0]["totalWeightKg"], 280)

	def test_inquiry_carries_the_items_pieces_and_sqft(self):
		frappe.db.set_value("Item", self.item, "custom_pieces_per_box", 4)
		frappe.db.set_value("Item", self.item, "custom_sqft_per_box", 15.5)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="WhatsApp")
		row = inquiry["items"][0]
		self.assertEqual(row["piecesPerBox"], 4)
		self.assertEqual(row["totalPieces"], 40)
		self.assertEqual(row["sqftPerBox"], 15.5)
		self.assertEqual(row["totalSqft"], 155)
		# sqm is derived from sqft (1 sqft = 0.09290304 sqm exactly), not its own field.
		self.assertAlmostEqual(row["sqmPerBox"], 1.44, places=2)
		self.assertAlmostEqual(row["totalSqm"], 14.4, places=2)

	def test_update_inquiry_patches_status_and_remarks(self):
		# "Available" etc. are per-item availability_status values now, not valid
		# parent Inquiry.status options -- exercise a real workflow transition instead.
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=50, source="Phone")
		updated = inquiry_api.update_inquiry(inquiry["id"], {"status": "Quoted", "remarks": "Confirmed in stock"})
		self.assertEqual(updated["status"], "Quoted")
		self.assertEqual(updated["remarks"], "Confirmed in stock")

	def test_write_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")

	def test_convert_to_purchase_requirement_creates_po_and_maps_inquiry(self):
		# An Inquiry stays "Open" at the parent level regardless of per-item stock
		# availability (that's the item row's own availability_status) -- no status
		# update needed before raising the purchase requirement.
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=40, source="Phone")

		po = inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], supplier=self.supplier, expected_ready_date=str(add_days(today(), 30)))

		self.assertEqual(po["lines"][0]["orderedQty"], 40)
		self.assertEqual(po["sourceInquiry"], inquiry["id"])
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Mapped to PO")

	def test_convert_to_purchase_requirement_falls_back_to_the_items_default_supplier(self):
		frappe.db.set_value("Item", self.item, "custom_default_supplier", self.supplier)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=40, source="Phone")

		po = inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], expected_ready_date=str(add_days(today(), 30)))

		self.assertEqual(po["supplier"], self.supplier)

	def test_convert_to_purchase_requirement_requires_a_supplier_when_none_can_be_resolved(self):
		# Its own item: another test sets a default supplier on self.item, which would then
		# be resolved here. (This used to pass only because a stale date failed first.)
		item = make_item("INQ-NO-SUPPLIER-ITEM", "Vitrified")
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=40, source="Phone")

		with self.assertRaises(frappe.ValidationError):
			inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], expected_ready_date=str(add_days(today(), 30)))

	def test_convert_to_purchase_requirement_rejects_ineligible_status(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		inquiry_api.update_inquiry(inquiry["id"], {"status": "Converted to Order"})

		with self.assertRaises(frappe.ValidationError):
			inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], supplier=self.supplier, expected_ready_date=str(add_days(today(), 30)))

	def test_create_inquiry_allows_item_outside_dealer_catalog(self):
		# ISS-2026-00644: a dealer may inquire on an item Purchase hasn't assigned to
		# their Dealer Catalog yet -- only ordering it stays gated to the assigned set
		# (see dealer_portal_api.convert_to_order / comms.flow_api's WhatsApp order
		# path), not inquiry creation.
		unassigned_item = make_item("INQ-UNASSIGNED-ITEM", "Vitrified")
		other_dealer = make_dealer("Inquiry Test Dealer 2")
		# Assigning anything at all to other_dealer creates its Dealer Catalog record,
		# which switches it off the unassigned-dealer full-catalog fallback.
		dealer_catalog_api.set_product_visibility(other_dealer, self.item, True)
		self.assertFalse(dealer_catalog_api.is_visible(other_dealer, unassigned_item))

		inquiry = inquiry_api.create_inquiry(dealer=other_dealer, item=unassigned_item, qty=5, source="Phone")
		self.assertEqual(inquiry["items"][0]["productId"], unassigned_item)

	def test_create_inquiry_rejects_pulled_back_item(self):
		item = make_item("INQ-PULLED-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_discontinuation_status", "Pulled Back")

		with self.assertRaises(frappe.ValidationError):
			inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=5, source="Phone")

	def test_create_inquiry_rejection_uses_the_configured_white_label_wording(self):
		settings = frappe.get_single("DMS Catalog Settings")
		settings.append("status_labels", {"status": "Pulled Back", "label": "PDC"})
		settings.save(ignore_permissions=True)
		try:
			item = make_item("INQ-PULLED-LABELLED", "Vitrified")
			frappe.db.set_value("Item", item, "custom_discontinuation_status", "Pulled Back")

			with self.assertRaises(frappe.ValidationError) as ctx:
				inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=5, source="Phone")
			self.assertIn("PDC", str(ctx.exception))
		finally:
			settings = frappe.get_single("DMS Catalog Settings")
			settings.status_labels = []
			settings.save(ignore_permissions=True)

	def test_create_inquiry_accepts_a_custom_style_request_with_no_item(self):
		# ISS-2026-00636 / MOM 3.6 -- a design not in the item master at all.
		inquiry = inquiry_api.create_inquiry(
			dealer=self.dealer, qty=1, source="WhatsApp", requested_style="Carrara Marble Look, 600x1200"
		)
		row = inquiry["items"][0]
		self.assertIsNone(row["productId"])
		self.assertEqual(row["requestedStyle"], "Carrara Marble Look, 600x1200")
		self.assertEqual(inquiry["status"], "Open")
		# No real item -- nothing to price, and prices is keyed by real item rows only.
		self.assertEqual(inquiry["prices"], {})
		self.assertIsNone(row["weightPerBoxKg"])
		# Explicitly never mirrored to an Opportunity -- there's no real item_code
		# to put in Opportunity Item, and the ticket itself says not to create one.
		self.assertIsNone(inquiry["linkedOpportunity"])

	def test_create_inquiry_rejects_both_item_and_requested_style(self):
		with self.assertRaises(frappe.ValidationError):
			inquiry_api.create_inquiry(
				dealer=self.dealer, item=self.item, qty=1, source="WhatsApp", requested_style="Something custom"
			)

	def test_create_inquiry_rejects_neither_item_nor_requested_style(self):
		with self.assertRaises(frappe.ValidationError):
			inquiry_api.create_inquiry(dealer=self.dealer, qty=1, source="WhatsApp")

	def test_search_finds_a_custom_style_inquiry_by_what_was_typed(self):
		# ISS-2026-00636: no item to match on, so search has to look at the style text.
		inquiry = inquiry_api.create_inquiry(
			dealer=self.dealer, qty=1, source="WhatsApp", requested_style="Onyx gold vein 800x1600"
		)
		found = [i["id"] for i in inquiry_api.list_inquiries(search="onyx gold", limit=50)["items"]]
		self.assertIn(inquiry["id"], found)

	def test_custom_style_inquiry_cannot_be_sent_to_purchase(self):
		inquiry = inquiry_api.create_inquiry(
			dealer=self.dealer, qty=3, source="WhatsApp", requested_style="Terrazzo chips, white"
		)
		with self.assertRaises(frappe.ValidationError):
			inquiry_api.convert_to_purchase_requirement(
				inquiry=inquiry["id"], expected_ready_date=add_days(today(), 10)
			)

	def test_convert_to_order_rejects_a_custom_style_inquiry(self):
		from dms_erp.auth.dealer_api import _dealer_portal_user
		from dms_erp.sales import dealer_portal_api

		# Created as staff (the only role allowed to call create_inquiry directly) --
		# a dealer never creates one of these themselves; comms.flow_api's own
		# WhatsApp endpoint is the real caller, and it goes through _create_inquiry
		# directly, same as this.
		inquiry = inquiry_api.create_inquiry(
			dealer=self.dealer, qty=1, source="WhatsApp", requested_style="Wood-look plank, grey"
		)

		dealer_user = _dealer_portal_user(self.dealer)
		frappe.set_user(dealer_user)
		try:
			with self.assertRaises(frappe.ValidationError):
				dealer_portal_api.convert_to_order(
					inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-CUSTOM-STYLE"
				)
		finally:
			frappe.set_user("Administrator")

	def test_list_inquiries_is_paginated_and_searchable(self):
		dealer = make_dealer("Inquiry Pagination Dealer")
		for _ in range(3):
			inquiry_api.create_inquiry(dealer=dealer, item=self.item, qty=1, source="Phone")

		page = inquiry_api.list_inquiries(dealer=dealer, limit=2, offset=0)
		self.assertEqual(page["total"], 3)
		self.assertEqual(len(page["items"]), 2)
		self.assertEqual(page["limit"], 2)
		self.assertEqual(page["offset"], 0)

		next_page = inquiry_api.list_inquiries(dealer=dealer, limit=2, offset=2)
		self.assertEqual(len(next_page["items"]), 1)

		found = inquiry_api.list_inquiries(dealer=dealer, search=self.item)
		self.assertEqual(found["total"], 3)

	def test_list_all_inquiries_returns_the_full_unpaginated_set(self):
		dealer = make_dealer("Inquiry Unpaginated Dealer")
		for _ in range(3):
			inquiry_api.create_inquiry(dealer=dealer, item=self.item, qty=1, source="Phone")

		self.assertEqual(len(inquiry_api.list_all_inquiries(dealer=dealer)), 3)


class TestInquiryDropReason(FrappeTestCase):
	"""BRD C.2.3 -- dropped inquiries record a reason. Pacific chose a warning (in the
	staff app), not a block, for closing without a customer PO."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		cls.dealer = make_dealer("Inquiry Drop Reason Dealer")
		cls.item = make_item("INQ-DROP-ITEM", "Vitrified")

	def _inquiry(self):
		return inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=5, source="Phone")

	def test_closing_with_a_listed_reason_records_it(self):
		closed = inquiry_api.update_inquiry(self._inquiry()["id"], {"status": "Closed", "dropReason": "Bought elsewhere"})
		self.assertEqual((closed["status"], closed["dropReason"]), ("Closed", "Bought elsewhere"))

	def test_other_needs_details_and_an_unknown_reason_is_refused(self):
		inquiry = self._inquiry()["id"]
		with self.assertRaises(frappe.ValidationError):
			inquiry_api.update_inquiry(inquiry, {"status": "Rejected", "dropReason": "Other"})
		with self.assertRaises(frappe.ValidationError):
			inquiry_api.update_inquiry(inquiry, {"status": "Rejected", "dropReason": "Made up reason"})
		rejected = inquiry_api.update_inquiry(inquiry, {"status": "Rejected", "dropReason": "Other", "dropReasonNote": "Changed design"})
		self.assertEqual(rejected["dropReasonNote"], "Changed design")

	def test_closing_without_a_reason_is_still_allowed(self):
		# A warning, not a block -- and other callers that close inquiries keep working.
		self.assertEqual(inquiry_api.update_inquiry(self._inquiry()["id"], {"status": "Closed"})["status"], "Closed")

	def test_reason_list_comes_from_settings_with_other_last(self):
		self.assertEqual(inquiry_api.list_inquiry_drop_reasons()[-1], "Other")
		frappe.db.set_single_value("DMS Sales Settings", "inquiry_drop_reasons", "Too costly\nOther\nNo stock")
		try:
			self.assertEqual(inquiry_api.list_inquiry_drop_reasons(), ["Too costly", "No stock", "Other"])
		finally:
			frappe.db.set_single_value("DMS Sales Settings", "inquiry_drop_reasons", "")

	def test_customer_po_can_be_set_on_the_inquiry(self):
		updated = inquiry_api.update_inquiry(self._inquiry()["id"], {"customerPo": " PO-881 "})
		self.assertEqual(updated["customerPo"], "PO-881")
