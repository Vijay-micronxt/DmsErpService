import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.reports import forecasting
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestForecasting(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Forecasting Test Supplier")
		cls.dealer = make_dealer("Forecasting Test Dealer")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_demand_forecast_projects_trailing_average_forward(self):
		item = make_item("FORECAST-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")

		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=120, source="Phone")
		order_api.create_order(dealer=self.dealer, lines=[{"item": item, "qty": 120}], expected_dispatch="2026-09-01", inquiry=inquiry["id"])

		result = forecasting.demand_forecast(weeks_ahead=4)
		row = next(r for r in result if r["productId"] == item)
		self.assertEqual(row["avgWeeklyQty"], round(120 / 12, 1))
		self.assertEqual(row["projectedQty"], round(round(120 / 12, 1) * 4))
		self.assertEqual(row["confidence"], "low — no seasonality or trend modeled")

	def test_demand_forecast_rejects_non_positive_weeks_ahead(self):
		with self.assertRaises(frappe.ValidationError):
			forecasting.demand_forecast(weeks_ahead=0)

	def test_demand_forecast_excludes_bulk_channel_sales(self):
		item = make_item("FORECAST-BULK-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")

		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=900, source="Phone")
		order_api.create_order(
			dealer=self.dealer, lines=[{"item": item, "qty": 900}], expected_dispatch="2026-09-01", inquiry=inquiry["id"], channel="Bulk"
		)

		result = forecasting.demand_forecast()
		self.assertFalse(any(r["productId"] == item for r in result))
