import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import order_api, reservation
from dms_erp.sales.setup import setup_sales
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_dealer_addresses,
	make_item,
	make_supplier,
)


class TestReservation(FrappeTestCase):
	"""ISS-2026-00711 (supersedes ISS-2026-00635's flag-only rule -- client's final
	decision, 5 Nov 2026). Every check runs review_reservations with an `as_of`
	date relative to the order's own (today's) transaction_date, rather than
	back-dating orders -- the same day arithmetic, without touching submitted
	documents."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_sales()
		cls.supplier = make_supplier("Reservation Test Supplier")
		cls.dealer = make_dealer("Reservation Test Dealer")
		cls.billing_address, cls.shipping_address = make_dealer_addresses(cls.dealer)
		cls.item = make_item("RESERVATION-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")
		frappe.db.set_single_value("DMS Sales Settings", "reservation_max_days", 14)
		frappe.db.set_single_value("DMS Sales Settings", "reservation_reminder_day", 7)

	def _order(self) -> str:
		return order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": 5}],
			expected_dispatch=add_days(today(), 30),
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
		)["id"]

	def _review(self, days_from_today: int) -> dict:
		return reservation.review_reservations(as_of=add_days(today(), days_from_today))

	def test_nothing_happens_before_the_reminder_day(self):
		order = self._order()
		result = self._review(6)
		self.assertNotIn(order, result["reminded"])
		self.assertNotIn(order, result["cancelled"])

	def test_reminder_on_day_7_then_auto_cancelled_after_day_14(self):
		order = self._order()

		self.assertIn(order, self._review(7)["reminded"])
		self.assertEqual(
			getdate(frappe.db.get_value("Sales Order", order, "custom_reservation_reminder_sent_on")),
			getdate(add_days(today(), 7)),
		)
		# Day 14 is still inside the window; the reminder isn't repeated either.
		day_14 = self._review(14)
		self.assertNotIn(order, day_14["reminded"])
		self.assertNotIn(order, day_14["cancelled"])

		self.assertIn(order, self._review(15)["cancelled"])
		# ISS-2026-00711 -- actually cancelled now, not just flagged.
		self.assertEqual(frappe.db.get_value("Sales Order", order, "custom_fulfillment_stage"), "Cancelled")
		self.assertEqual(order_api.get_order(order)["reservation"]["state"], "Closed")

	def test_cancelled_once_only(self):
		order = self._order()
		self.assertIn(order, self._review(30)["cancelled"])
		# Already Cancelled, so the main stage filter excludes it on the next run.
		self.assertNotIn(order, self._review(31)["cancelled"])

	def test_an_order_already_past_the_window_is_cancelled_without_a_reminder(self):
		order = self._order()
		result = self._review(40)
		self.assertIn(order, result["cancelled"])
		self.assertNotIn(order, result["reminded"])

	def test_dispatched_orders_are_left_alone(self):
		order = self._order()
		frappe.db.set_value("Sales Order", order, "custom_fulfillment_stage", "Dispatched")
		result = self._review(30)
		self.assertNotIn(order, result["cancelled"])
		self.assertEqual(order_api.get_order(order)["reservation"]["state"], "Closed")
