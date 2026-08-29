import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import api as catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase.reorder_api import SAFETY_STOCK_BOXES, SALES_VELOCITY_WINDOW_DAYS, reorder_suggestions
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_dealer, make_item, make_supplier


class TestReorder(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Reorder Test Supplier")
		cls.dealer = make_dealer("Reorder Test Dealer")
		cls.bay = make_bay("REORDER-A-01", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def _suggestion_for(self, item_code):
		return next(s for s in reorder_suggestions() if s["productId"] == item_code)

	def test_low_stock_item_is_watch_with_positive_suggested_qty(self):
		item = make_item("REORDER-LOW", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="REORDER-LOW-B1", total_qty=20, lines=[{"bay": "REORDER-A-01", "qty": 20}], supplier=self.supplier
		)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["currentStock"], 20)
		self.assertGreater(suggestion["suggestedQty"], 0)
		self.assertEqual(suggestion["urgency"], "Watch")
		self.assertIn(f"Below {SAFETY_STOCK_BOXES}-box safety stock", suggestion["reasons"])

	def test_well_stocked_item_is_healthy_with_no_suggestion(self):
		item = make_item("REORDER-HEALTHY", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="REORDER-HEALTHY-B1", total_qty=500, lines=[{"bay": "REORDER-A-01", "qty": 500}], supplier=self.supplier
		)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["currentStock"], 500)
		self.assertEqual(suggestion["suggestedQty"], 0)
		self.assertEqual(suggestion["urgency"], "Healthy")

	def test_non_reorderable_item_never_gets_a_suggestion(self):
		item = make_item("REORDER-PULLED", "Vitrified")
		catalog_api.update_product(item, {"status": "Pulled Back"})

		suggestion = self._suggestion_for(item)
		self.assertTrue(suggestion["nonReorderable"])
		self.assertEqual(suggestion["suggestedQty"], 0)
		self.assertEqual(suggestion["urgency"], "Healthy")

	def test_pending_and_missed_inquiry_qty_are_real(self):
		item = make_item("REORDER-INQUIRY", "Vitrified")

		open_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=30, source="Phone")
		out_of_stock_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=15, source="WhatsApp")
		inquiry_api.update_inquiry(out_of_stock_inquiry["id"], {"status": "Out of Stock"})
		closed_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=999, source="Phone")
		inquiry_api.update_inquiry(closed_inquiry["id"], {"status": "Closed"})

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["pendingInquiryQty"], 30)
		self.assertEqual(suggestion["missedDemandQty"], 15)
		self.assertEqual(suggestion["urgency"], "Critical")  # zero stock + missed demand
		self.assertIn("15 boxes of missed/constrained retail demand", suggestion["reasons"])
		self.assertIn("30 boxes in open retail inquiries", suggestion["reasons"])
		self.assertEqual(open_inquiry["status"], "Open")

	def test_recent_retail_sales_qty_feeds_lead_time_demand(self):
		item = make_item("REORDER-VELOCITY", "Vitrified")
		frappe.db.set_value("Item", item, "lead_time_days", 30)
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		allocation_api.create_allocation(
			item=item, batch_no="REORDER-VELOCITY-B1", total_qty=200, lines=[{"bay": "REORDER-A-01", "qty": 200}], supplier=self.supplier
		)

		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=60, source="Phone")
		order_api.create_order(dealer=self.dealer, lines=[{"item": item, "qty": 60}], expected_dispatch="2026-09-01", inquiry=inquiry["id"])

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["recentRetailSalesQty"], 60)
		expected_lead_time_demand = round((60 / SALES_VELOCITY_WINDOW_DAYS) * 30)
		self.assertGreater(expected_lead_time_demand, 0)
		self.assertTrue(any(f"{expected_lead_time_demand} boxes of expected demand" in r for r in suggestion["reasons"]))
