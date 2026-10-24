import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog import api as catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase import po_api
from dms_erp.purchase.reorder_api import (
	DEFAULT_TARGET_STOCK_MONTHS,
	SALES_VELOCITY_WINDOW_DAYS,
	notify_reorder_review,
	reorder_suggestions,
)
from dms_erp.purchase.setup import setup_purchase
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_bay,
	make_dealer,
	make_item,
	make_supplier,
)

# Relative to today: a fixed date rots into the past, and ERPNext then refuses the order
# ("Expected Delivery Date should be after Sales Order Date").
DISPATCH = str(add_days(today(), 30))


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
		cls.purchase_user = _make_role_user("reorder.purchase.tester@pacific.test", "DMS Purchase")
		# The MOQ tests set this per test; start from "no MOQ" whatever the site has.
		frappe.db.set_single_value("DMS Purchase Settings", "default_moq", 0)
		cls.target_months = (
			frappe.db.get_single_value("DMS Purchase Settings", "target_stock_months") or DEFAULT_TARGET_STOCK_MONTHS
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Notification Log", {"for_user": self.purchase_user})

	def _suggestion_for(self, item_code):
		return next(s for s in reorder_suggestions() if s["productId"] == item_code)

	def _stock(self, item, qty):
		# Its own bay per item: one shared bay filled up across the suite ("only 0 boxes free").
		bay = f"{item}-BAY"
		make_bay(bay, categories=["Vitrified"])
		allocation_api.create_allocation(
			item=item, batch_no=f"{item}-B1", total_qty=qty, lines=[{"bay": bay, "qty": qty}], supplier=self.supplier
		)

	def _price(self, item):
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")

	def _sell(self, item, qty, channel=None):
		"""A submitted Sales Order straight from staff -- no inquiry, so the sale doesn't
		also count as pending/missed inquiry demand."""
		kwargs = {"channel": channel} if channel else {}
		return order_api.create_order(dealer=self.dealer, lines=[{"item": item, "qty": qty}], expected_dispatch=DISPATCH, **kwargs)

	def _missed_demand(self, item, qty):
		"""An inquiry on a zero-stock item, which create_inquiry auto-sets to Out of Stock."""
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=qty, source="Phone")
		self.assertEqual(inquiry["status"], "Out of Stock")
		return inquiry

	def _target(self, sold_in_window):
		# BRD C.4.1: average monthly retail sales x the configured months (reorder_api._suggestion_for).
		return round(sold_in_window / (SALES_VELOCITY_WINDOW_DAYS / 30) * self.target_months)

	def test_low_stock_item_is_watch_with_positive_suggested_qty(self):
		item = make_item("REORDER-LOW", "Vitrified")
		frappe.db.set_value("Item", item, "lead_time_days", 0)
		self._price(item)
		self._sell(item, 180)
		self._stock(item, 20)

		suggestion = self._suggestion_for(item)
		target = self._target(180)
		self.assertGreater(target, 20)
		self.assertEqual(suggestion["currentStock"], 20)
		self.assertEqual(suggestion["recentRetailSalesQty"], 180)
		# Rounded UP to the next 5 (never down -- that would under-order).
		self.assertEqual(suggestion["suggestedQty"], -(-(target - 20) // 5) * 5)
		self.assertEqual(suggestion["urgency"], "Watch")
		self.assertTrue(any(f"Below the {target}-box target" in r for r in suggestion["reasons"]))

	def test_well_stocked_item_is_healthy_with_no_suggestion(self):
		item = make_item("REORDER-HEALTHY", "Vitrified")
		self._stock(item, 500)

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

		# Zero stock throughout -- create_inquiry auto-derives "Out of Stock" from real
		# on-hand qty (see inquiry_api._create_inquiry), so this covers that derivation
		# feeding missedDemandQty without a manual status override.
		pending_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=30, source="Phone")
		inquiry_api.update_inquiry(pending_inquiry["id"], {"status": "Quoted"})  # staff quoted it manually
		self._missed_demand(item, 15)
		closed_inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=999, source="Phone")
		inquiry_api.update_inquiry(closed_inquiry["id"], {"status": "Closed"})

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["pendingInquiryQty"], 30)
		self.assertEqual(suggestion["missedDemandQty"], 15)
		self.assertEqual(suggestion["suggestedQty"], 45)  # 15 + 30; zero stock, no sales history
		self.assertEqual(suggestion["urgency"], "Critical")  # zero stock + missed demand
		self.assertTrue(any("boxes of missed/constrained retail demand" in r for r in suggestion["reasons"]))
		self.assertTrue(any("boxes in open retail inquiries" in r for r in suggestion["reasons"]))

	def test_recent_retail_sales_qty_feeds_lead_time_demand(self):
		item = make_item("REORDER-VELOCITY", "Vitrified")
		frappe.db.set_value("Item", item, "lead_time_days", 30)
		self._price(item)
		self._stock(item, 200)
		self._sell(item, 60)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["recentRetailSalesQty"], 60)
		expected_lead_time_demand = round((60 / SALES_VELOCITY_WINDOW_DAYS) * 30)
		self.assertGreater(expected_lead_time_demand, 0)
		self.assertTrue(any(f"{expected_lead_time_demand} boxes of expected demand" in r for r in suggestion["reasons"]))

	def test_recent_retail_sales_qty_excludes_bulk_channel_orders(self):
		item = make_item("REORDER-BULK-EXCLUDED", "Vitrified")
		self._price(item)
		self._sell(item, 40)
		self._sell(item, 900, channel="Bulk")

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["recentRetailSalesQty"], 40)  # the 900-box Bulk order never counts

	def test_open_purchase_order_qty_nets_out_of_suggested_qty(self):
		item = make_item("REORDER-OPEN-PO", "Vitrified")
		self._missed_demand(item, 25)

		before = self._suggestion_for(item)
		self.assertEqual(before["openPurchaseOrderQty"], 0)
		self.assertEqual(before["suggestedQty"], 25)

		po = po_api.create_purchase_order(item=item, ordered_qty=25, supplier=self.supplier, expected_ready_date=DISPATCH)

		after = self._suggestion_for(item)
		self.assertEqual(after["openPurchaseOrderQty"], 25)
		self.assertEqual(after["openPurchaseOrders"], [{"po": po["id"], "pendingQty": 25}])
		self.assertEqual(after["suggestedQty"], 0)
		self.assertTrue(any("already on order" in r for r in after["reasons"]))

	def test_suggested_qty_rounds_up_to_the_next_5(self):
		# BRD C.4.1: round UP to the nearest 5. A raw need of 102 (missed demand, zero
		# stock, no sales history) is 105 -- 100 if rounded to nearest, 110 at /10.
		item = make_item("REORDER-ROUND5", "Vitrified")
		self._missed_demand(item, 102)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["suggestedQty"], 105)

	def test_suggested_qty_is_raised_to_the_items_own_moq(self):
		item = make_item("REORDER-MOQ-ITEM", "Vitrified")
		self._missed_demand(item, 10)
		frappe.db.set_value("Item", item, "custom_moq", 150)

		suggestion = self._suggestion_for(item)
		# The raw need is 10, well under the 150-box MOQ that clamps it up.
		self.assertEqual(suggestion["suggestedQty"], 150)
		self.assertIn("Raised to the 150-box MOQ", suggestion["reasons"])

	def test_suggested_qty_falls_back_to_the_site_wide_default_moq(self):
		item = make_item("REORDER-MOQ-DEFAULT", "Vitrified")
		self._missed_demand(item, 10)
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
		self._stock(item, 500)
		frappe.db.set_value("Item", item, "custom_moq", 150)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["suggestedQty"], 0)

	def test_notify_reorder_review_saves_a_draft_plan_and_notifies_purchase(self):
		item = make_item("REORDER-NOTIFY", "Vitrified")
		self._missed_demand(item, 40)
		suggestion = self._suggestion_for(item)
		idle = {**suggestion, "suggestedQty": 0}

		notified = notify_reorder_review(suggestions=[suggestion, idle])
		self.assertGreaterEqual(notified, 1)  # every enabled DMS Purchase / Management user

		logs = frappe.get_all("Notification Log", filters={"for_user": self.purchase_user}, pluck="subject")
		self.assertEqual(len(logs), 1)
		self.assertRegex(logs[0], r"^Reorder Plan .+: 1 item\(s\) need review$")
		plan = logs[0].split(":")[0].removeprefix("Reorder Plan ")
		self.assertEqual(frappe.db.get_value("Reorder Plan", plan, "status"), "Draft")
		self.assertEqual(frappe.get_all("Reorder Plan Item", filters={"parent": plan}, pluck="item"), [item])

	def test_notify_reorder_review_is_a_no_op_when_nothing_is_pending(self):
		notified = notify_reorder_review(suggestions=[{"suggestedQty": 0}])
		self.assertEqual(notified, 0)
		self.assertFalse(frappe.get_all("Notification Log", filters={"for_user": self.purchase_user}))
