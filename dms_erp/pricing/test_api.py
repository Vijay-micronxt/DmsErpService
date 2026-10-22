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
		for price_list in ("Pricing Tag Override List", "Pricing Tag Empty List"):
			if frappe.db.exists("Price List", price_list):
				frappe.delete_doc("Price List", price_list, force=True, ignore_permissions=True)

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

	def test_a_manual_price_list_tag_always_wins_over_the_classification_tier(self):
		item = make_item("PRICE-TAG-OVERRIDE", "Vitrified")
		dealer = make_dealer("Pricing Tag Override Dealer")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")
		# Dealer defaults to Standard Dealer tier (no rate published for it) but is
		# bumped to Master Dealer by sales volume -- the tag must still beat even a
		# real, non-default classification, not just the Standard Dealer default.
		frappe.db.set_value("Customer", dealer, "custom_dealer_classification", DEALER_CLASSIFICATION_MASTER)
		pricing_api.set_price_for_list(item, DEALER_CLASSIFICATION_MASTER, 700)

		frappe.get_doc(
			{"doctype": "Price List", "price_list_name": "Pricing Tag Override List", "selling": 1, "enabled": 1, "currency": "INR"}
		).insert(ignore_permissions=True)
		pricing_api.set_price_for_list(item, "Pricing Tag Override List", 999)
		frappe.db.set_value("Customer", dealer, "default_price_list", "Pricing Tag Override List")

		self.assertEqual(pricing_api.get_price_for_dealer(item, dealer), 999)

	def test_a_manual_price_list_tag_still_falls_back_to_the_dealer_list_when_unpriced(self):
		item = make_item("PRICE-TAG-FALLBACK", "Vitrified")
		dealer = make_dealer("Pricing Tag Fallback Dealer")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")

		frappe.get_doc(
			{"doctype": "Price List", "price_list_name": "Pricing Tag Empty List", "selling": 1, "enabled": 1, "currency": "INR"}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", dealer, "default_price_list", "Pricing Tag Empty List")

		# The tagged list has no rate for this item at all -- falls back to the flat
		# "Dealer" list rather than returning no price for a dealer who has a tag set.
		self.assertEqual(pricing_api.get_price_for_dealer(item, dealer), 500)


class TestSuggestPriceFromHistory(FrappeTestCase):
	"""ISS-2026-00623 -- Purchase Invoice/Sales Invoice history is real, migration-only
	data this app never creates itself (see pricing/README.md's own note), so there's
	no existing test fixture anywhere in this suite for building one -- the "real
	history produces a sane suggestion" path was instead verified directly against the
	actual migrated site's data this session (median purchase cost/margin computed
	correctly for several real items; a deliberately sparse item, with only one
	real data point on each side, correctly returned None). What IS covered here,
	with no Purchase/Sales Invoice fixture needed: the pure median helper, and the
	"no history at all" honest-absence case."""

	def test_median_handles_even_and_odd_counts_and_a_single_value(self):
		self.assertEqual(pricing_api._median([10]), 10)
		self.assertEqual(pricing_api._median([10, 20, 30]), 20)
		self.assertEqual(pricing_api._median([10, 20, 30, 40]), 25)
		# Order-independent.
		self.assertEqual(pricing_api._median([30, 10, 20]), 20)

	def test_suggest_price_from_history_returns_none_for_an_item_with_no_history(self):
		ensure_company()
		item = make_item("PRICE-HISTORY-NONE", "Vitrified")
		self.assertIsNone(pricing_api.suggest_price_from_history(item))

	def test_suggest_price_from_history_returns_none_for_a_nonexistent_item(self):
		self.assertIsNone(pricing_api.suggest_price_from_history("NO-SUCH-ITEM-AT-ALL"))
