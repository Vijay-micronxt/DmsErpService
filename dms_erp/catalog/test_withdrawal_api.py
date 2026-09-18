import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog import dealer_catalog_api, series_api
from dms_erp.catalog import withdrawal_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier

SERIES = "Withdrawal Test Series"


class TestWithdrawalApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Withdrawal Test Supplier")
		cls.dealer = make_dealer("Withdrawal Test Dealer")

	def tearDown(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("Series", SERIES):
			frappe.delete_doc("Series", SERIES, force=True, ignore_permissions=True)

	def _make_series(self, **overrides):
		params = {
			"series_name": SERIES,
			"withdrawal_automation_enabled": True,
			"withdrawal_no_sale_days_threshold": 14,
			"withdrawal_min_store_count": 3,
			"withdrawal_min_annual_sales_boxes": 100,
		}
		params.update(overrides)
		if frappe.db.exists("Series", SERIES):
			frappe.delete_doc("Series", SERIES, force=True, ignore_permissions=True)
		return series_api.create_series(**params)

	def _delivered_sale(self, item, qty, days_ago):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=qty, source="Phone")
		order = order_api.create_order(
			dealer=self.dealer, lines=[{"item": item, "qty": qty}], expected_dispatch=today(), inquiry=inquiry["id"]
		)
		for stage in ("Picking", "Ready to Dispatch", "Dispatched", "Delivered"):
			order_api.advance_order_stage(order["id"], stage)
		frappe.db.set_value("Sales Order", order["id"], "transaction_date", add_days(today(), -days_ago))
		return order["id"]

	def test_advances_status_when_all_three_signals_breach(self):
		self._make_series()
		item = make_item("WITHDRAW-BREACH-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_series_ref", SERIES)
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		self._delivered_sale(item, qty=10, days_ago=30)  # idle 30d >= 14d, 10 boxes < 100

		moved = withdrawal_api.evaluate_product_withdrawals()

		self.assertEqual(moved, 1)
		self.assertEqual(frappe.db.get_value("Item", item, "custom_discontinuation_status"), "Partially Discontinued")

	def test_skips_when_recently_sold(self):
		self._make_series()
		item = make_item("WITHDRAW-RECENT-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_series_ref", SERIES)
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		self._delivered_sale(item, qty=10, days_ago=2)  # idle only 2d < 14d threshold

		moved = withdrawal_api.evaluate_product_withdrawals()

		self.assertEqual(moved, 0)
		self.assertEqual(frappe.db.get_value("Item", item, "custom_discontinuation_status"), "Active")

	def test_skips_when_store_count_meets_threshold(self):
		self._make_series()
		item = make_item("WITHDRAW-INSTORE-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_series_ref", SERIES)
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		self._delivered_sale(item, qty=10, days_ago=30)
		for i in range(3):  # meets the min_store_count=3 threshold
			dealer = make_dealer(f"Withdrawal In-Store Dealer {i}")
			dealer_catalog_api.set_product_visibility(dealer=dealer, item=item, visible=True)

		moved = withdrawal_api.evaluate_product_withdrawals()

		self.assertEqual(moved, 0)
		self.assertEqual(frappe.db.get_value("Item", item, "custom_discontinuation_status"), "Active")

	def test_skips_when_annual_sales_meets_threshold(self):
		self._make_series(withdrawal_min_annual_sales_boxes=5)
		item = make_item("WITHDRAW-SELLING-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_series_ref", SERIES)
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		self._delivered_sale(item, qty=10, days_ago=30)  # 10 boxes >= 5 threshold

		moved = withdrawal_api.evaluate_product_withdrawals()

		self.assertEqual(moved, 0)
		self.assertEqual(frappe.db.get_value("Item", item, "custom_discontinuation_status"), "Active")

	def test_ignores_series_missing_any_threshold(self):
		self._make_series(withdrawal_min_store_count=0)  # opted in but incomplete
		item = make_item("WITHDRAW-INCOMPLETE-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_series_ref", SERIES)
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		self._delivered_sale(item, qty=10, days_ago=30)

		moved = withdrawal_api.evaluate_product_withdrawals()

		self.assertEqual(moved, 0)
		self.assertEqual(frappe.db.get_value("Item", item, "custom_discontinuation_status"), "Active")

	def test_never_advances_past_display_removal_pending(self):
		self._make_series()
		item = make_item("WITHDRAW-TERMINAL-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_series_ref", SERIES)
		frappe.db.set_value("Item", item, "custom_discontinuation_status", "Display Removal Pending")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		self._delivered_sale(item, qty=10, days_ago=30)

		moved = withdrawal_api.evaluate_product_withdrawals()

		self.assertEqual(moved, 0)
		self.assertEqual(
			frappe.db.get_value("Item", item, "custom_discontinuation_status"), "Display Removal Pending"
		)

	def test_series_api_round_trips_withdrawal_thresholds(self):
		created = self._make_series()
		self.assertTrue(created["withdrawalAutomationEnabled"])
		self.assertEqual(created["withdrawalNoSaleDaysThreshold"], 14)
		self.assertEqual(created["withdrawalMinStoreCount"], 3)
		self.assertEqual(created["withdrawalMinAnnualSalesBoxes"], 100)

		updated = series_api.update_series(SERIES, {"withdrawalMinStoreCount": 7})
		self.assertEqual(updated["withdrawalMinStoreCount"], 7)
