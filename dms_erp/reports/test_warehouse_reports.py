import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.reports import warehouse_reports
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_dealer, make_item, make_supplier


class TestWarehouseReports(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Warehouse Reports Test Supplier")
		cls.item = make_item("WREPORT-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		cls.bay = make_bay("WREPORT-A-01", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_bay_occupancy_report_includes_summary(self):
		allocation_api.create_allocation(
			item=self.item, batch_no="WREPORT-BATCH-1", total_qty=100, lines=[{"bay": "WREPORT-A-01", "qty": 100}], supplier=self.supplier
		)

		result = warehouse_reports.bay_occupancy_report(bay_type="main")
		row = next(r for r in result["rows"] if r["code"] == "WREPORT-A-01")
		self.assertEqual(row["occupiedBoxes"], 100)
		self.assertEqual(result["summary"]["bayCount"], len(result["rows"]))

	def test_visual_stock_balance_nests_lots_under_their_bay(self):
		allocation_api.create_allocation(
			item=self.item, batch_no="WREPORT-BATCH-2", total_qty=50, lines=[{"bay": "WREPORT-A-01", "qty": 50}], supplier=self.supplier
		)

		result = warehouse_reports.visual_stock_balance()
		bay_row = next(b for b in result if b["code"] == "WREPORT-A-01")
		self.assertTrue(any(l["batchNumber"] == "WREPORT-BATCH-2" for l in bay_row["lots"]))

	def test_stock_clearance_suggestions_flags_aged_overstocked_slow_item(self):
		item = make_item("WREPORT-CLEARANCE-ITEM", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="WREPORT-CLEARANCE-B1", total_qty=250, lines=[{"bay": "WREPORT-A-01", "qty": 250}], supplier=self.supplier
		)
		# Backdate the ledger entries directly — create_allocation always posts at
		# today(), and there's no parameter to backdate it through the real API.
		frappe.db.sql(
			"update `tabStock Ledger Entry` set posting_date = %s where item_code = %s", (add_days(today(), -100), item)
		)

		result = warehouse_reports.stock_clearance_suggestions()
		row = next(r for r in result if r["productId"] == item)
		self.assertEqual(row["currentStock"], 250)
		self.assertEqual(row["recentSalesQty"], 0)
		self.assertGreaterEqual(row["oldestBatchAgeDays"], 100)

	def test_stock_clearance_suggestions_excludes_well_stocked_recent_item(self):
		item = make_item("WREPORT-FRESH-ITEM", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="WREPORT-FRESH-B1", total_qty=250, lines=[{"bay": "WREPORT-A-01", "qty": 250}], supplier=self.supplier
		)

		result = warehouse_reports.stock_clearance_suggestions()
		self.assertFalse(any(r["productId"] == item for r in result))

	def test_display_replacement_suggestions_pairs_slow_display_item_with_fast_mover(self):
		# Own category (not "Vitrified") so this test's velocity ranking can't be
		# skewed by unrelated Vitrified-category sales created in other test files.
		make_bay("WREPORT-DISPLAY-01", bay_type="display", categories=["Outdoor / Parking"])
		dealer = make_dealer("Warehouse Reports Display Dealer")

		slow_item = make_item("WREPORT-DISPLAY-SLOW", "Outdoor / Parking")
		allocation_api.create_allocation(
			item=slow_item, batch_no="WREPORT-DISPLAY-B1", total_qty=10, lines=[{"bay": "WREPORT-DISPLAY-01", "qty": 10}], supplier=self.supplier
		)

		fast_item = make_item("WREPORT-DISPLAY-FAST", "Outdoor / Parking")
		pricing_api.ensure_price_record(fast_item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=fast_item, final_price=360, reason="Launch")
		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=fast_item, qty=150, source="Phone")
		order_api.create_order(dealer=dealer, lines=[{"item": fast_item, "qty": 150}], expected_dispatch="2026-09-01", inquiry=inquiry["id"])

		result = warehouse_reports.display_replacement_suggestions()
		row = next(r for r in result if r["currentDisplayItem"] == slow_item)
		self.assertEqual(row["bayId"], frappe.db.get_value("Warehouse", {"custom_bay_code": "WREPORT-DISPLAY-01"}, "name"))
		self.assertEqual(row["suggestedReplacement"], fast_item)

	def test_display_replacement_suggestions_skips_actively_selling_display_item(self):
		make_bay("WREPORT-DISPLAY-02", bay_type="display", categories=["Outdoor / Parking"])
		dealer = make_dealer("Warehouse Reports Display Dealer 2")

		selling_item = make_item("WREPORT-DISPLAY-SELLING", "Outdoor / Parking")
		pricing_api.ensure_price_record(selling_item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=selling_item, final_price=360, reason="Launch")
		allocation_api.create_allocation(
			item=selling_item, batch_no="WREPORT-DISPLAY-B2", total_qty=10, lines=[{"bay": "WREPORT-DISPLAY-02", "qty": 10}], supplier=self.supplier
		)
		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=selling_item, qty=5, source="Phone")
		order_api.create_order(dealer=dealer, lines=[{"item": selling_item, "qty": 5}], expected_dispatch="2026-09-01", inquiry=inquiry["id"])

		result = warehouse_reports.display_replacement_suggestions()
		self.assertFalse(any(r["currentDisplayItem"] == selling_item for r in result))
