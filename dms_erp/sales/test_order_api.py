import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, order_api, picking_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestOrderApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Order Test Supplier")
		cls.dealer = make_dealer("Order Test Dealer")
		cls.item = make_item("ORDER-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _make_order(self, qty=10):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=qty, source="Phone")
		return order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": qty}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)

	def test_create_order_from_inquiry_uses_approved_price_directly(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=40, source="Phone")
		order = order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": 40}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)

		self.assertEqual(order["lines"][0]["rate"], 360)
		self.assertEqual(order["sourceType"], "Inquiry")
		self.assertEqual(order["sourceRef"], inquiry["id"])
		self.assertEqual(order["stage"], "Confirmed")
		self.assertEqual(order["channel"], "Retail")
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Converted to Order")

	def test_create_order_accepts_bulk_channel(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=500, source="Phone")
		order = order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": 500}], expected_dispatch="2026-09-01", inquiry=inquiry["id"], channel="Bulk"
		)
		self.assertEqual(order["channel"], "Bulk")

	def test_create_order_rejects_invalid_channel(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		with self.assertRaises(frappe.ValidationError):
			order_api.create_order(
				dealer=self.dealer, lines=[{"item": self.item, "qty": 10}], expected_dispatch="2026-09-01", inquiry=inquiry["id"], channel="Wholesale"
			)

	def test_advance_order_stage_follows_forward_flow(self):
		order = self._make_order()

		updated = order_api.advance_order_stage(order["id"], "Picking")
		self.assertEqual(updated["stage"], "Picking")
		self.assertEqual(len(updated["history"]), 3)  # Created, Confirmed, Picking

		updated = order_api.advance_order_stage(order["id"], "Ready to Dispatch")
		self.assertEqual(updated["stage"], "Ready to Dispatch")

	def test_advance_order_stage_rejects_skipping_ahead(self):
		order = self._make_order()
		with self.assertRaises(frappe.ValidationError):
			order_api.advance_order_stage(order["id"], "Dispatched")

	def test_advance_order_stage_allows_cancel_but_not_after_delivered(self):
		order = self._make_order()
		cancelled = order_api.advance_order_stage(order["id"], "Cancelled")
		self.assertEqual(cancelled["stage"], "Cancelled")

		order2 = self._make_order()
		for stage in ("Picking", "Ready to Dispatch", "Dispatched", "Delivered"):
			order_api.advance_order_stage(order2["id"], stage)
		with self.assertRaises(frappe.ValidationError):
			order_api.advance_order_stage(order2["id"], "Cancelled")

	def test_entering_picking_stage_creates_pick_tasks(self):
		order = self._make_order(qty=25)
		order_api.advance_order_stage(order["id"], "Picking")

		tasks = picking_api.list_pick_tasks(order["id"])
		self.assertEqual(len(tasks), 1)
		self.assertEqual(tasks[0]["itemCode"], self.item)
		self.assertEqual(tasks[0]["qty"], 25)
		self.assertEqual(tasks[0]["status"], "Pending")

	def test_write_requires_sales_or_management_role(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=1, source="Phone")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			order_api.create_order(dealer=self.dealer, lines=[{"item": self.item, "qty": 1}], expected_dispatch="2026-09-01", inquiry=inquiry["id"])

	def test_list_orders_is_paginated_and_searchable(self):
		dealer = make_dealer("Order Pagination Dealer")
		item = make_item("ORDER-PAGINATION-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")

		created = []
		for _ in range(3):
			inquiry = inquiry_api.create_inquiry(dealer=dealer, item=item, qty=10, source="Phone")
			created.append(
				order_api.create_order(dealer=dealer, lines=[{"item": item, "qty": 10}], expected_dispatch="2026-09-01", inquiry=inquiry["id"])
			)

		page = order_api.list_orders(dealer=dealer, limit=2, offset=0)
		self.assertEqual(page["total"], 3)
		self.assertEqual(len(page["items"]), 2)

		next_page = order_api.list_orders(dealer=dealer, limit=2, offset=2)
		self.assertEqual(len(next_page["items"]), 1)

		found = order_api.list_orders(dealer=dealer, search=created[0]["id"])
		self.assertEqual(found["total"], 1)
		self.assertEqual(found["items"][0]["id"], created[0]["id"])
