import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import order_api, reservation
from dms_erp.sales.setup import setup_sales
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestReservation(FrappeTestCase):
	"""ISS-2026-00635. Every check runs review_reservations with an `as_of` date
	relative to the order's own (today's) transaction_date, rather than back-dating
	orders -- the same day arithmetic, without touching submitted documents."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_sales()
		cls.supplier = make_supplier("Reservation Test Supplier")
		cls.dealer = make_dealer("Reservation Test Dealer")
		cls.item = make_item("RESERVATION-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")
		frappe.db.set_single_value("DMS Sales Settings", "reservation_max_days", 21)
		frappe.db.set_single_value("DMS Sales Settings", "reservation_reminder_day", 20)

	def _order(self) -> str:
		return order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": 5}], expected_dispatch=add_days(today(), 30)
		)["id"]

	def _review(self, days_from_today: int) -> dict:
		return reservation.review_reservations(as_of=add_days(today(), days_from_today))

	def test_nothing_happens_before_the_reminder_day(self):
		order = self._order()
		result = self._review(19)
		self.assertNotIn(order, result["reminded"])
		self.assertNotIn(order, result["flagged"])

	def test_reminder_on_day_20_then_flag_only_after_day_21(self):
		order = self._order()

		self.assertIn(order, self._review(20)["reminded"])
		self.assertEqual(
			getdate(frappe.db.get_value("Sales Order", order, "custom_reservation_reminder_sent_on")),
			getdate(add_days(today(), 20)),
		)
		# Day 21 is still inside the window; the reminder isn't repeated either.
		day_21 = self._review(21)
		self.assertNotIn(order, day_21["reminded"])
		self.assertNotIn(order, day_21["flagged"])

		self.assertIn(order, self._review(22)["flagged"])
		self.assertEqual(order_api.get_order(order)["reservation"]["state"], "Flagged for Review")
		# Flag only -- never cancelled.
		self.assertEqual(frappe.db.get_value("Sales Order", order, "docstatus"), 1)
		self.assertNotEqual(frappe.db.get_value("Sales Order", order, "custom_fulfillment_stage"), "Cancelled")

	def test_flagged_once_only(self):
		order = self._order()
		self.assertIn(order, self._review(30)["flagged"])
		self.assertNotIn(order, self._review(31)["flagged"])

	def test_an_order_already_past_the_window_is_flagged_without_a_reminder(self):
		order = self._order()
		result = self._review(40)
		self.assertIn(order, result["flagged"])
		self.assertNotIn(order, result["reminded"])

	def test_dispatched_orders_are_left_alone(self):
		order = self._order()
		frappe.db.set_value("Sales Order", order, "custom_fulfillment_stage", "Dispatched")
		result = self._review(30)
		self.assertNotIn(order, result["flagged"])
		self.assertEqual(order_api.get_order(order)["reservation"]["state"], "Closed")
