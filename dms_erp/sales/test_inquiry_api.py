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

	def test_create_inquiry_with_no_stock_is_out_of_stock(self):
		# BRD C.2.4 / Phase 22 -- status is derived from real on-hand qty at creation
		# (previously always hardcoded "Open"), which is what feeds the reorder
		# engine's missed-demand signal (purchase.reorder_api.MISSED_DEMAND_STATUSES).
		item = make_item("INQ-TEST-NO-STOCK", "Vitrified")
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=100, source="WhatsApp")
		self.assertEqual(inquiry["status"], "Out of Stock")
		self.assertEqual(inquiry["dealerId"], self.dealer)
		self.assertEqual(inquiry["qty"], 100)
		self.assertIsNone(inquiry["duplicateOf"])

	def test_create_inquiry_fully_covered_by_stock_is_available(self):
		item = make_item("INQ-TEST-FULL-STOCK", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="INQ-TEST-FULL-B1", total_qty=100, lines=[{"bay": self.bay["code"], "qty": 100}], supplier=self.supplier
		)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=40, source="Phone")
		self.assertEqual(inquiry["status"], "Available")

	def test_create_inquiry_partially_covered_by_stock_is_partially_available(self):
		item = make_item("INQ-TEST-PARTIAL-STOCK", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="INQ-TEST-PARTIAL-B1", total_qty=20, lines=[{"bay": self.bay["code"], "qty": 20}], supplier=self.supplier
		)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=40, source="Phone")
		self.assertEqual(inquiry["status"], "Partially Available")

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
		self.assertEqual(inquiry["weightPerBoxKg"], 28)
		self.assertEqual(inquiry["totalWeightKg"], 280)

	def test_inquiry_carries_the_items_pieces_and_sqft(self):
		frappe.db.set_value("Item", self.item, "custom_pieces_per_box", 4)
		frappe.db.set_value("Item", self.item, "custom_sqft_per_box", 15.5)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="WhatsApp")
		self.assertEqual(inquiry["piecesPerBox"], 4)
		self.assertEqual(inquiry["totalPieces"], 40)
		self.assertEqual(inquiry["sqftPerBox"], 15.5)
		self.assertEqual(inquiry["totalSqft"], 155)

	def test_update_inquiry_patches_status_and_remarks(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=50, source="Phone")
		updated = inquiry_api.update_inquiry(inquiry["id"], {"status": "Available", "remarks": "Confirmed in stock"})
		self.assertEqual(updated["status"], "Available")
		self.assertEqual(updated["remarks"], "Confirmed in stock")

	def test_write_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")

	def test_convert_to_purchase_requirement_creates_po_and_maps_inquiry(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=40, source="Phone")
		inquiry_api.update_inquiry(inquiry["id"], {"status": "Out of Stock"})

		po = inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], supplier=self.supplier, expected_ready_date="2026-09-01")

		self.assertEqual(po["lines"][0]["orderedQty"], 40)
		self.assertEqual(po["sourceInquiry"], inquiry["id"])
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Mapped to PO")

	def test_convert_to_purchase_requirement_rejects_ineligible_status(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		inquiry_api.update_inquiry(inquiry["id"], {"status": "Converted to Order"})

		with self.assertRaises(frappe.ValidationError):
			inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], supplier=self.supplier, expected_ready_date="2026-09-01")

	def test_create_inquiry_rejects_item_outside_dealer_catalog(self):
		restricted_item = make_item("INQ-RESTRICTED-ITEM", "Vitrified")
		other_dealer = make_dealer("Inquiry Test Dealer 2")
		# Assigning anything at all to other_dealer creates its Dealer Catalog record,
		# which switches it off the unassigned-dealer full-catalog fallback.
		dealer_catalog_api.set_product_visibility(other_dealer, self.item, True)

		with self.assertRaises(frappe.PermissionError):
			inquiry_api.create_inquiry(dealer=other_dealer, item=restricted_item, qty=5, source="Phone")

	def test_create_inquiry_rejects_pulled_back_item(self):
		item = make_item("INQ-PULLED-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_discontinuation_status", "Pulled Back")

		with self.assertRaises(frappe.ValidationError):
			inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=5, source="Phone")

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
