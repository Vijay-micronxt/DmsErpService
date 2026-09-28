import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import api as catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase import po_api
from dms_erp.purchase.reorder_api import (
	SAFETY_STOCK_BOXES,
	SALES_VELOCITY_WINDOW_DAYS,
	notify_reorder_review,
	reorder_suggestions,
)
from dms_erp.purchase.setup import setup_purchase
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_dealer, make_item, make_supplier


def _make_role_user(email, role):
	if frappe.db.exists("User", email):
		return email
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"roles": [{"role": role}],
		}
	)
	user.insert(ignore_permissions=True)
	return user.name


class TestReorder(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		setup_purchase()
		cls.supplier = make_supplier("Reorder Test Supplier")
		cls.dealer = make_dealer("Reorder Test Dealer")
		cls.bay = make_bay("REORDER-A-01", categories=["Vitrified"])
		cls.purchase_user = _make_role_user("reorder.purchase.tester@pacific.test", "DMS Purchase")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Notification Log", {"for_user": self.purchase_user})

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

	def test_suggestion_carries_the_items_default_supplier(self):
		item = make_item("REORDER-DEFAULT-SUPPLIER", "Vitrified")
		suggestion = self._suggestion_for(item)
		self.assertIsNone(suggestion["defaultSupplier"])

		frappe.db.set_value("Item", item, "custom_default_supplier", self.supplier)
		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["defaultSupplier"], self.supplier)

	def test_non_reorderable_item_never_gets_a_suggestion(self):
		item = make_item("REORDER-PULLED", "Vitrified")
		catalog_api.update_product(item, {"status": "Pulled Back"})

		suggestion = self._suggestion_for(item)
		self.assertTrue(suggestion["nonReorderable"])
		self.assertEqual(suggestion["suggestedQty"], 0)
		self.assertEqual(suggestion["urgency"], "Healthy")

	def test_pending_and_missed_inquiry_qty_are_real(self):
		item = make_item("REORDER-INQUIRY", "Vitrified")

		# Zero stock throughout -- create_inquiry now auto-derives "Out of Stock"
		# from real on-hand qty (see inquiry_api._create_inquiry), so this covers
		# that derivation feeding missedDemandQty without a manual status override.
		pending_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=30, source="Phone")
		inquiry_api.update_inquiry(pending_inquiry["id"], {"status": "Quoted"})  # staff quoted it manually
		out_of_stock_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=15, source="WhatsApp")
		closed_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=999, source="Phone")
		inquiry_api.update_inquiry(closed_inquiry["id"], {"status": "Closed"})

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["pendingInquiryQty"], 30)
		self.assertEqual(suggestion["missedDemandQty"], 15)
		self.assertEqual(suggestion["urgency"], "Critical")  # zero stock + missed demand
		self.assertIn("15 boxes of missed/constrained retail demand", suggestion["reasons"])
		self.assertIn("30 boxes in open retail inquiries", suggestion["reasons"])
		self.assertEqual(out_of_stock_inquiry["status"], "Out of Stock")

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

	def test_recent_retail_sales_qty_excludes_bulk_channel_orders(self):
		item = make_item("REORDER-BULK-EXCLUDED", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")

		retail_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=40, source="Phone")
		order_api.create_order(dealer=self.dealer, lines=[{"item": item, "qty": 40}], expected_dispatch="2026-09-01", inquiry=retail_inquiry["id"])

		bulk_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=900, source="Phone")
		order_api.create_order(
			dealer=self.dealer, lines=[{"item": item, "qty": 900}], expected_dispatch="2026-09-01", inquiry=bulk_inquiry["id"], channel="Bulk"
		)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["recentRetailSalesQty"], 40)  # the 900-box Bulk order never counts

	def test_open_purchase_order_qty_nets_out_of_suggested_qty(self):
		item = make_item("REORDER-OPEN-PO", "Vitrified")

		before = self._suggestion_for(item)
		self.assertEqual(before["openPurchaseOrderQty"], 0)
		self.assertGreater(before["suggestedQty"], 0)

		po = po_api.create_purchase_order(item=item, ordered_qty=before["suggestedQty"], supplier=self.supplier, expected_ready_date="2026-09-01")

		after = self._suggestion_for(item)
		self.assertEqual(after["openPurchaseOrderQty"], before["suggestedQty"])
		self.assertEqual(after["openPurchaseOrders"], [{"po": po["id"], "pendingQty": before["suggestedQty"]}])
		self.assertEqual(after["suggestedQty"], 0)
		self.assertTrue(any("already on order" in r for r in after["reasons"]))

	def test_suggested_qty_rounds_to_the_nearest_5_not_10(self):
		# BRD says round to the nearest 5; raw_need=107 (100 safety stock + 7 missed
		# demand, zero stock) rounds to 105 at /5 but would have rounded to 110 at /10 --
		# this distinguishes the two so a regression back to /10 is caught.
		item = make_item("REORDER-ROUND5", "Vitrified")
		out_of_stock_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=7, source="Phone")
		inquiry_api.update_inquiry(out_of_stock_inquiry["id"], {"status": "Out of Stock"})

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["suggestedQty"], 105)

	def test_suggested_qty_is_raised_to_the_items_own_moq(self):
		item = make_item("REORDER-MOQ-ITEM", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="REORDER-MOQ-ITEM-B1", total_qty=90, lines=[{"bay": "REORDER-A-01", "qty": 90}], supplier=self.supplier
		)
		frappe.db.set_value("Item", item, "custom_moq", 150)

		suggestion = self._suggestion_for(item)
		# Without the MOQ, this item's raw need (100 safety stock - 90 in stock = 10,
		# rounds to 10) is well under the 150-box MOQ that should clamp it up.
		self.assertEqual(suggestion["suggestedQty"], 150)
		self.assertIn("Raised to the 150-box MOQ", suggestion["reasons"])

	def test_suggested_qty_falls_back_to_the_site_wide_default_moq(self):
		item = make_item("REORDER-MOQ-DEFAULT", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="REORDER-MOQ-DEFAULT-B1", total_qty=90, lines=[{"bay": "REORDER-A-01", "qty": 90}], supplier=self.supplier
		)
		frappe.db.set_single_value("DMS Purchase Settings", "default_moq", 50)
		try:
			suggestion = self._suggestion_for(item)
		finally:
			frappe.db.set_single_value("DMS Purchase Settings", "default_moq", 0)

		self.assertEqual(suggestion["suggestedQty"], 50)
		self.assertIn("Raised to the 50-box MOQ", suggestion["reasons"])

	def test_zero_suggested_qty_is_never_raised_to_the_moq(self):
		# A well-stocked item that needs nothing shouldn't be forced into a reorder
		# just because a site-wide or item MOQ exists.
		item = make_item("REORDER-MOQ-ZERO", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="REORDER-MOQ-ZERO-B1", total_qty=500, lines=[{"bay": "REORDER-A-01", "qty": 500}], supplier=self.supplier
		)
		frappe.db.set_value("Item", item, "custom_moq", 150)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["suggestedQty"], 0)

	def test_notify_reorder_review_notifies_purchase_and_management_when_items_are_pending(self):
		notified = notify_reorder_review(suggestions=[{"suggestedQty": 40}, {"suggestedQty": 0}])
		self.assertEqual(notified, 1)  # only the DMS Purchase test user is seeded in this suite

		logs = frappe.get_all("Notification Log", filters={"for_user": self.purchase_user}, pluck="subject")
		self.assertEqual(logs, ["1 item(s) need reorder plan review"])

	def test_notify_reorder_review_is_a_no_op_when_nothing_is_pending(self):
		notified = notify_reorder_review(suggestions=[{"suggestedQty": 0}])
		self.assertEqual(notified, 0)
		self.assertFalse(frappe.get_all("Notification Log", filters={"for_user": self.purchase_user}))
