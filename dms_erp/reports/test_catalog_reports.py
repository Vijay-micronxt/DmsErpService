import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.reports import catalog_reports
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestCatalogReports(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Catalog Reports Test Supplier")
		cls.dealer = make_dealer("Catalog Reports Test Dealer")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_pricing_and_csp_report_computes_actual_margin(self):
		item = make_item("CREPORT-PRICED-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")  # no freight/handling/other -> landingCost=400
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")

		result = catalog_reports.pricing_and_csp_report()
		row = next(r for r in result if r["productId"] == item)
		self.assertEqual(row["landingCost"], 400)
		self.assertEqual(row["csp"], 500)  # suggestedPrice = landingCost * (1 + 25%)
		self.assertEqual(row["actualMarginPct"], 20.0)  # (500-400)/500 * 100

	def test_pricing_and_csp_report_excludes_pending_records(self):
		item = make_item("CREPORT-PENDING-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")

		result = catalog_reports.pricing_and_csp_report()
		self.assertNotIn(item, [r["productId"] for r in result])

	def test_product_movement_report_ranks_by_velocity(self):
		fast_item = make_item("CREPORT-FAST-ITEM", "Vitrified")
		slow_item = make_item("CREPORT-SLOW-ITEM", "Vitrified")
		for item in (fast_item, slow_item):
			pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
			pricing_api.approve_price(item=item, final_price=360, reason="Launch")

		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=fast_item, qty=200, source="Phone")
		order_api.create_order(dealer=self.dealer, lines=[{"item": fast_item, "qty": 200}], expected_dispatch="2026-09-01", inquiry=inquiry["id"])

		fastest_first = catalog_reports.product_movement_report(order="fast")
		fast_idx = next(i for i, r in enumerate(fastest_first) if r["productId"] == fast_item)
		slow_idx = next(i for i, r in enumerate(fastest_first) if r["productId"] == slow_item)
		self.assertLess(fast_idx, slow_idx)
