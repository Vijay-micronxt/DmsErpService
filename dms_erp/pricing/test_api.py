import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse.test_fixtures import ensure_company, make_item, make_supplier


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
