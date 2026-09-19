import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import series_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.dealer_classification import DEALER_CLASSIFICATION_MASTER
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestPricingApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Pricing Pagination Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("Product Series", "Pricing Test Series"):
			frappe.delete_doc("Product Series", "Pricing Test Series", force=True, ignore_permissions=True)

	def test_list_price_records_is_paginated(self):
		before = pricing_api.list_price_records()
		baseline_total = before["total"]

		items = [make_item(f"PRICE-PAGE-{i}", "Vitrified") for i in range(3)]
		for item in items:
			pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")

		page = pricing_api.list_price_records(limit=2, offset=0)
		self.assertEqual(page["total"], baseline_total + 3)
		self.assertEqual(len(page["items"]), 2)
		self.assertEqual(page["limit"], 2)
		self.assertEqual(page["offset"], 0)

		all_records = pricing_api.list_all_price_records()
		self.assertEqual(len(all_records), baseline_total + 3)
		self.assertTrue({r["productId"] for r in all_records}.issuperset(set(items)))

	def test_get_price_for_dealer_falls_back_to_the_dealer_list_when_no_tier_rate_exists(self):
		item = make_item("PRICE-TIER-FALLBACK", "Vitrified")
		dealer = make_dealer("Pricing Tier Fallback Dealer")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")

		# The dealer defaults to Standard Dealer, which has no rate published for this
		# item (no Series at all here) -- falls back to the flat "Dealer" list price.
		self.assertEqual(pricing_api.get_price_for_dealer(item, dealer), 500)

	def test_approve_price_publishes_series_tier_rates_onto_the_item(self):
		series_api.create_series(
			series_name="Pricing Test Series",
			price_list_rates=[{"price_list": DEALER_CLASSIFICATION_MASTER, "rate": 700}],
		)
		item = make_item("PRICE-TIER-SERIES", "Vitrified")
		frappe.db.set_value("Item", item, "custom_series_ref", "Pricing Test Series")
		dealer = make_dealer("Pricing Tier Series Dealer")
		frappe.db.set_value("Customer", dealer, "custom_dealer_classification", DEALER_CLASSIFICATION_MASTER)
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")

		pricing_api.approve_price(item=item, final_price=500, reason="Launch")

		self.assertEqual(pricing_api.get_dealer_price(item), 500)
		self.assertEqual(pricing_api.get_dealer_price(item, price_list=DEALER_CLASSIFICATION_MASTER), 700)
		self.assertEqual(pricing_api.get_price_for_dealer(item, dealer), 700)

	def test_get_dealer_tier_prices_returns_all_three_lists_with_none_for_unpublished(self):
		item = make_item("PRICE-TIER-READ", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")

		prices = pricing_api.get_dealer_tier_prices(item)

		self.assertEqual(prices, {"Standard Dealer": None, "Dealer": 500, "Master Dealer": None})

	def test_set_dealer_tier_price_writes_a_single_list_without_disturbing_the_others(self):
		item = make_item("PRICE-TIER-WRITE", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")

		result = pricing_api.set_dealer_tier_price(item=item, price_list=DEALER_CLASSIFICATION_MASTER, rate=650)

		self.assertEqual(result, {"Standard Dealer": None, "Dealer": 500, "Master Dealer": 650})

	def test_set_dealer_tier_price_rejects_an_unknown_price_list(self):
		item = make_item("PRICE-TIER-INVALID", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		with self.assertRaises(frappe.ValidationError):
			pricing_api.set_dealer_tier_price(item=item, price_list="Wholesale", rate=650)

	def test_set_dealer_tier_price_requires_purchase_or_management_role(self):
		item = make_item("PRICE-TIER-PERM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			pricing_api.set_dealer_tier_price(item=item, price_list=DEALER_CLASSIFICATION_MASTER, rate=650)
