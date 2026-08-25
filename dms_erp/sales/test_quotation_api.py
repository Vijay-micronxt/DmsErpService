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

		dealer_catalog_api.set_product_visibility(cls.dealer, cls.priced_item, True)
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.unpriced_item, True)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_quotation_applies_markup_to_approved_price(self):
		quotation = quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 100}], markup_pct=12
		)
		self.assertEqual(quotation["lines"][0]["rate"], 560)  # round(500 * 1.12)

	def test_create_quotation_rejects_item_outside_dealer_catalog(self):
		other_dealer = make_dealer("Quotation Test Dealer 2")
		with self.assertRaises(frappe.PermissionError):
			quotation_api.create_quotation(dealer=other_dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)

	def test_create_quotation_rejects_unpriced_item(self):
		with self.assertRaises(frappe.ValidationError):
			quotation_api.create_quotation(dealer=self.dealer, lines=[{"item": self.unpriced_item, "qty": 10}], markup_pct=10)

	def test_create_quotation_from_inquiry_marks_it_quoted(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=50, source="Phone")
		quotation_api.create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 50}], markup_pct=10, inquiry=inquiry["id"]
		)
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Quoted")

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
