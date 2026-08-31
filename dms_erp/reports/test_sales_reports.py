import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.comms import api as comms_api
from dms_erp.reports import sales_reports
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestSalesReports(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Sales Reports Test Supplier")
		cls.dealer = make_dealer("Sales Reports Test Dealer")
		cls.item = make_item("SREPORT-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=400, reason="Launch")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_dealer_inquiry_report_summarizes_by_status(self):
		i1 = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone", expected_delivery="2026-09-01")
		i2 = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=20, source="Phone", expected_delivery="2026-09-01")
		inquiry_api.update_inquiry(i2["id"], {"status": "Closed"})

		result = sales_reports.dealer_inquiry_report(dealer=self.dealer)
		ids = {r["id"] for r in result["rows"]}
		self.assertIn(i1["id"], ids)
		self.assertIn(i2["id"], ids)
		self.assertEqual(result["summary"]["byStatus"].get("Closed"), 1)

	def test_dealer_inquiry_report_filters_by_date_range(self):
		inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=5, source="Phone")

		result = sales_reports.dealer_inquiry_report(dealer=self.dealer, from_date="2099-01-01", to_date="2099-12-31")
		self.assertEqual(result["summary"]["total"], 0)

	def test_missed_demand_report_prices_out_of_stock_inquiries(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=15, source="Phone")
		inquiry_api.update_inquiry(inquiry["id"], {"status": "Out of Stock"})

		result = sales_reports.missed_demand_report()
		row = next(r for r in result["rows"] if r["id"] == inquiry["id"])
		self.assertEqual(row["estimatedValue"], 15 * 400)
		self.assertGreaterEqual(result["totalValue"], 15 * 400)

	def test_missed_demand_report_excludes_open_inquiries(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=15, source="Phone")

		result = sales_reports.missed_demand_report()
		ids = {r["id"] for r in result["rows"]}
		self.assertNotIn(inquiry["id"], ids)

	def test_retail_vs_bulk_report_groups_by_channel(self):
		retail_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		order_api.create_order(dealer=self.dealer, lines=[{"item": self.item, "qty": 10}], expected_dispatch="2026-09-01", inquiry=retail_inquiry["id"])

		bulk_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=800, source="Phone")
		order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": 800}], expected_dispatch="2026-09-01", inquiry=bulk_inquiry["id"], channel="Bulk"
		)

		result = sales_reports.retail_vs_bulk_report()
		by_channel = {r["channel"]: r for r in result["byChannel"]}
		self.assertGreaterEqual(by_channel["Retail"]["orderCount"], 1)
		self.assertGreaterEqual(by_channel["Bulk"]["orderCount"], 1)

	def test_dealer_activity_report_rolls_up_inquiries_orders_and_messages(self):
		activity_dealer = make_dealer("Sales Reports Activity Dealer")
		inquiry_api.create_inquiry(dealer=activity_dealer, item=self.item, qty=10, source="Phone")
		order_inquiry = inquiry_api.create_inquiry(dealer=activity_dealer, item=self.item, qty=20, source="Phone")
		order_api.create_order(
			dealer=activity_dealer, lines=[{"item": self.item, "qty": 20}], expected_dispatch="2026-09-01", inquiry=order_inquiry["id"]
		)
		comms_api.send_message(dealer=activity_dealer, text="Following up on your order")

		result = sales_reports.dealer_activity_report(dealer=activity_dealer)
		row = result[0]
		self.assertEqual(row["dealerId"], activity_dealer)
		self.assertEqual(row["inquiryCount"], 2)
		self.assertEqual(row["orderCount"], 1)
		self.assertEqual(row["orderValue"], 20 * 400)
		self.assertEqual(row["messageCount"], 1)
		self.assertIsNotNone(row["lastContact"])

	def test_duplicate_inquiry_report_flags_repeated_open_inquiries(self):
		dup_dealer = make_dealer("Sales Reports Duplicate Dealer")
		i1 = inquiry_api.create_inquiry(dealer=dup_dealer, item=self.item, qty=10, source="Phone")
		i2 = inquiry_api.create_inquiry(dealer=dup_dealer, item=self.item, qty=15, source="WhatsApp")

		result = sales_reports.duplicate_inquiry_report()
		group = next(r for r in result if r["dealerId"] == dup_dealer and r["productId"] == self.item)
		self.assertEqual(group["count"], 2)
		self.assertEqual({i["id"] for i in group["inquiries"]}, {i1["id"], i2["id"]})

	def test_duplicate_inquiry_report_excludes_closed_inquiries(self):
		dealer = make_dealer("Sales Reports Non Duplicate Dealer")
		item = make_item("SREPORT-NODUP-ITEM", "Vitrified")
		i1 = inquiry_api.create_inquiry(dealer=dealer, item=item, qty=10, source="Phone")
		i2 = inquiry_api.create_inquiry(dealer=dealer, item=item, qty=15, source="Phone")
		inquiry_api.update_inquiry(i2["id"], {"status": "Closed"})

		result = sales_reports.duplicate_inquiry_report()
		self.assertFalse(any(r["dealerId"] == dealer and r["productId"] == item for r in result))
		self.assertTrue(i1)  # kept alive to document intent: only i2 was closed
