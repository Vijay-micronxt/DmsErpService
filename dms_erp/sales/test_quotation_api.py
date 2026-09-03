import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import dealer_catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, quotation_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestQuotationApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Quotation Test Supplier")
		cls.dealer = make_dealer("Quotation Test Dealer")

		cls.priced_item = make_item("QTN-PRICED", "Vitrified")
		pricing_api.ensure_price_record(cls.priced_item, cls.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=cls.priced_item, final_price=500, reason="Launch")

		cls.unpriced_item = make_item("QTN-UNPRICED", "Vitrified")

		cls.second_item = make_item("QTN-SECOND", "Vitrified")
		pricing_api.ensure_price_record(cls.second_item, cls.supplier, 200, 25, "2026-08-01")
		pricing_api.approve_price(item=cls.second_item, final_price=250, reason="Launch")

		dealer_catalog_api.set_product_visibility(cls.dealer, cls.priced_item, True)
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.unpriced_item, True)
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.second_item, True)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_quotation_applies_markup_to_approved_price(self):
		quotation = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 100}], markup_pct=12
		)
		self.assertEqual(quotation["lines"][0]["rate"], 560)  # round(500 * 1.12)
		self.assertEqual(quotation["channel"], "Retail")  # default

	def test_create_quotation_accepts_bulk_channel_and_carries_into_order(self):
		quotation = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 500}], markup_pct=8, channel="Bulk"
		)
		self.assertEqual(quotation["channel"], "Bulk")

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch="2026-09-01")
		self.assertEqual(order["channel"], "Bulk")

	def test_create_quotation_rejects_invalid_channel(self):
		with self.assertRaises(frappe.ValidationError):
			quotation_api.create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10, channel="Wholesale")

	def test_create_quotation_rejects_item_outside_dealer_catalog(self):
		other_dealer = make_dealer("Quotation Test Dealer 2")
		with self.assertRaises(frappe.PermissionError):
			quotation_api.create_quotation(dealer=other_dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)

	def test_create_quotation_rejects_unpriced_item(self):
		with self.assertRaises(frappe.ValidationError):
			quotation_api.create_quotation(dealer=self.dealer, lines=[{"item": self.unpriced_item, "qty": 10}], markup_pct=10)

	def test_create_quotation_rejects_pulled_back_item_even_when_still_visible(self):
		frappe.db.set_value("Item", self.priced_item, "custom_discontinuation_status", "Pulled Back")
		try:
			# Still assigned/visible — set_product_visibility isn't retroactively cleaned up.
			self.assertTrue(dealer_catalog_api.is_visible(self.dealer, self.priced_item))
			with self.assertRaises(frappe.ValidationError):
				quotation_api.create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)
		finally:
			frappe.db.set_value("Item", self.priced_item, "custom_discontinuation_status", "Active")

	def test_create_quotation_from_inquiry_marks_it_quoted(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=50, source="Phone")
		quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 50}], markup_pct=10, inquiry=inquiry["id"]
		)
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Quoted")

	def test_add_quotation_line_amends_and_reprices_every_line(self):
		quotation = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 100}], markup_pct=12
		)

		amended = quotation_api.add_quotation_line(quotation["id"], self.second_item, 20)

		self.assertNotEqual(amended["id"], quotation["id"])
		self.assertEqual(len(amended["lines"]), 2)
		by_item = {l["itemCode"]: l for l in amended["lines"]}
		self.assertEqual(by_item[self.priced_item]["rate"], 560)  # round(500 * 1.12), rebuilt not stale
		self.assertEqual(by_item[self.second_item]["rate"], 280)  # round(250 * 1.12)
		self.assertEqual(frappe.db.get_value("Quotation", quotation["id"], "docstatus"), 2)

	def test_remove_quotation_line_drops_it_and_rejects_last_line(self):
		quotation = quotation_api.create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 100}, {"item": self.second_item, "qty": 20}],
			markup_pct=10,
		)

		amended = quotation_api.remove_quotation_line(quotation["id"], self.second_item)
		self.assertEqual(len(amended["lines"]), 1)
		self.assertEqual(amended["lines"][0]["itemCode"], self.priced_item)

		with self.assertRaises(frappe.ValidationError):
			quotation_api.remove_quotation_line(amended["id"], self.priced_item)

	def test_update_quotation_line_qty_changes_only_that_line(self):
		quotation = quotation_api.create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 100}, {"item": self.second_item, "qty": 20}],
			markup_pct=10,
		)

		amended = quotation_api.update_quotation_line_qty(quotation["id"], self.second_item, 50)
		by_item = {l["itemCode"]: l for l in amended["lines"]}
		self.assertEqual(by_item[self.second_item]["qty"], 50)
		self.assertEqual(by_item[self.priced_item]["qty"], 100)

	def test_edit_rejects_already_ordered_quotation(self):
		quotation = quotation_api.create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)
		quotation_api.convert_to_order(quotation["id"], expected_dispatch="2026-09-01")

		with self.assertRaises(frappe.ValidationError):
			quotation_api.update_quotation_line_qty(quotation["id"], self.priced_item, 5)

	def test_update_quotation_status_marks_lost(self):
		quotation = quotation_api.create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)

		updated = quotation_api.update_quotation_status(quotation["id"], "Lost", detailed_reason="Dealer chose a competitor")
		self.assertEqual(updated["status"], "Lost")

	def test_update_quotation_status_rejects_other_statuses(self):
		quotation = quotation_api.create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)

		with self.assertRaises(frappe.ValidationError):
			quotation_api.update_quotation_status(quotation["id"], "Open")

	def test_convert_to_order_creates_sales_order_and_closes_inquiry(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=30, source="Phone")
		quotation = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 30}], markup_pct=15, inquiry=inquiry["id"]
		)

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch="2026-09-01")

		self.assertEqual(order["sourceType"], "Quotation")
		self.assertEqual(order["sourceRef"], quotation["id"])
		self.assertEqual(order["stage"], "Confirmed")
		self.assertEqual(len(order["history"]), 2)
		self.assertEqual(order["history"][0]["stage"], "Created")
		self.assertEqual(order["history"][1]["stage"], "Confirmed")
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Converted to Order")

	def test_list_quotations_is_paginated_and_searchable(self):
		dealer = make_dealer("Quotation Pagination Dealer")
		dealer_catalog_api.set_product_visibility(dealer, self.priced_item, True)
		created = [
			quotation_api.create_quotation(dealer=dealer, lines=[{"item": self.priced_item, "qty": 5}], markup_pct=10)
			for _ in range(3)
		]

		page = quotation_api.list_quotations(dealer=dealer, limit=2, offset=0)
		self.assertEqual(page["total"], 3)
		self.assertEqual(len(page["items"]), 2)

		next_page = quotation_api.list_quotations(dealer=dealer, limit=2, offset=2)
		self.assertEqual(len(next_page["items"]), 1)

		found = quotation_api.list_quotations(dealer=dealer, search=created[0]["id"])
		self.assertEqual(found["total"], 1)
		self.assertEqual(found["items"][0]["id"], created[0]["id"])
