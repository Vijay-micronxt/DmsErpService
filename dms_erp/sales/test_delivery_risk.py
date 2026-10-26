import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase import po_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.sales import delivery_risk, order_api
from dms_erp.sales.setup import setup_sales
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_item,
	make_supplier,
)

# Sales Order (and Purchase Order) reject a delivery/ready date that isn't strictly
# after today -- same constraint every other test file in this app already works
# around (see test_order_api.py's own DISPATCH comment). So every order here is
# created with its delivery_date fixed at today()+DISPATCH_DAYS (always valid,
# never rots into the past), and every check evaluates `as_of` that same future
# date -- exactly test_reservation.py's own technique, just anchored on
# delivery_date instead of order age.
DISPATCH_DAYS = 30


def _as_of():
	return add_days(today(), DISPATCH_DAYS)


class TestDeliveryRisk(FrappeTestCase):
	"""BRD C.3.2. Every check uses a fresh item per test (supply/demand are matched
	per item)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_purchase()
		setup_sales()
		cls.supplier = make_supplier("Delivery Risk Test Supplier")
		cls.dealer = make_dealer("Delivery Risk Test Dealer")

	def tearDown(self):
		frappe.db.set_single_value("DMS Purchase Settings", "po_delay_grace_days", 0)
		frappe.db.set_single_value("DMS Purchase Settings", "delivery_risk_eta_buffer_days", 0)

	def _item(self, suffix: str) -> str:
		item = make_item(f"DELIVRISK-{suffix}", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		return item

	def _po(self, item: str, qty: float, ready_in_days: int) -> dict:
		"""ready_in_days relative to today() (not _as_of()) -- a positive offset keeps
		PO creation itself valid; whether it reads as delayed/on-track depends on
		`as_of`, evaluated separately by at_risk_sales_order_lines."""
		return po_api.create_purchase_order(
			item=item, ordered_qty=qty, expected_ready_date=add_days(today(), ready_in_days), supplier=self.supplier
		)

	def _order(self, item: str, qty: float, delivery_in_days: int = DISPATCH_DAYS) -> str:
		return order_api.create_order(
			dealer=self.dealer, lines=[{"item": item, "qty": qty}], expected_dispatch=add_days(today(), delivery_in_days)
		)["id"]

	def _line(self, order: str) -> str:
		return frappe.db.get_value("Sales Order Item", {"parent": order}, "name")

	def test_on_track_po_is_not_flagged(self):
		item = self._item("ONTRACK")
		self._po(item, 50, DISPATCH_DAYS + 10)  # still 10 days out, as of DISPATCH_DAYS
		order = self._order(item, 20)

		rows = delivery_risk.at_risk_sales_order_lines(as_of=_as_of())
		row = next(r for r in rows if r["salesOrder"] == order)
		self.assertFalse(row["atRisk"])
		self.assertEqual(row["coveredFrom"], "factory_dispatch_ontrack")

	def test_delayed_po_with_imminent_delivery_date_is_flagged(self):
		item = self._item("IMMINENT")
		self._po(item, 50, DISPATCH_DAYS - 10)  # 10 days overdue, as of DISPATCH_DAYS
		order = self._order(item, 20)

		rows = delivery_risk.at_risk_sales_order_lines(as_of=_as_of())
		row = next(r for r in rows if r["salesOrder"] == order)
		self.assertTrue(row["atRisk"])
		self.assertEqual(row["coveredFrom"], "factory_dispatch_delayed")
		self.assertEqual(row["delayDays"], 10)

	def test_delayed_po_but_far_future_delivery_date_is_not_flagged(self):
		"""Step D: delayed supply alone isn't enough -- the order's own delivery date
		must actually be imminent. The PO is overdue as of DISPATCH_DAYS (same as the
		imminent-flag test above), but this order's own delivery_date is 170 days
		further out still, so there's no evidence yet the delay will still matter by
		the time it's actually due."""
		item = self._item("FARFUTURE")
		self._po(item, 50, DISPATCH_DAYS - 10)  # 10 days overdue, as of DISPATCH_DAYS
		order = self._order(item, 20, delivery_in_days=DISPATCH_DAYS + 170)

		rows = delivery_risk.at_risk_sales_order_lines(as_of=_as_of())
		row = next(r for r in rows if r["salesOrder"] == order)
		self.assertFalse(row["atRisk"])
		self.assertEqual(row["coveredFrom"], "factory_dispatch_delayed")  # still correctly tracked, just not risky yet

	def test_item_with_no_delayed_supply_is_never_flagged_even_if_uncovered(self):
		"""An uncovered line with no delay anywhere in the item's supply is a plain
		reorder gap (purchase.reorder_api's own territory), not a C.3.2 case."""
		item = self._item("NOPO")
		order = self._order(item, 20)  # no PO raised for this item at all

		rows = delivery_risk.at_risk_sales_order_lines(as_of=_as_of())
		row = next(r for r in rows if r["salesOrder"] == order)
		self.assertEqual(row["coveredFrom"], "uncovered")
		self.assertFalse(row["atRisk"])

	def test_fifo_first_order_safe_second_order_at_risk(self):
		"""One on-track PO sized to exactly cover the first (earlier) order, one
		delayed PO as the only remaining supply -- the later order is the one left
		drawing from the delayed pool, not because of its own PO, but because the
		earlier order already claimed the on-track supply first."""
		item = self._item("FIFO")
		self._po(item, 30, DISPATCH_DAYS + 10)  # on-track, exactly covers order 1
		self._po(item, 20, DISPATCH_DAYS - 5)  # delayed, the only supply left for order 2

		order_1 = self._order(item, 30)
		order_2 = self._order(item, 20)

		rows = delivery_risk.at_risk_sales_order_lines(as_of=_as_of())
		row_1 = next(r for r in rows if r["salesOrder"] == order_1)
		row_2 = next(r for r in rows if r["salesOrder"] == order_2)

		self.assertFalse(row_1["atRisk"])
		self.assertEqual(row_1["coveredFrom"], "factory_dispatch_ontrack")
		self.assertTrue(row_2["atRisk"])
		self.assertEqual(row_2["coveredFrom"], "factory_dispatch_delayed")

	def test_grace_period_absorbs_a_small_slip(self):
		item = self._item("GRACE")
		frappe.db.set_single_value("DMS Purchase Settings", "po_delay_grace_days", 5)
		self._po(item, 50, DISPATCH_DAYS - 3)  # only 3 days late as of DISPATCH_DAYS -- inside the 5-day grace
		order = self._order(item, 20)

		rows = delivery_risk.at_risk_sales_order_lines(as_of=_as_of())
		row = next(r for r in rows if r["salesOrder"] == order)
		self.assertFalse(row["atRisk"])
		self.assertEqual(row["coveredFrom"], "factory_dispatch_ontrack")  # not even counted as delayed

	def test_review_flags_once_and_does_not_renotify(self):
		item = self._item("FLAGONCE")
		self._po(item, 50, DISPATCH_DAYS - 10)
		order = self._order(item, 20)
		line = self._line(order)

		first = delivery_risk.review_delivery_risk(as_of=_as_of())
		self.assertIn(line, first["flagged"])
		self.assertGreater(first["notified"], 0)
		self.assertIsNotNone(frappe.db.get_value("Sales Order Item", line, "custom_delivery_risk_flagged_on"))

		second = delivery_risk.review_delivery_risk(as_of=_as_of())
		self.assertNotIn(line, second["flagged"])
		self.assertEqual(second["notified"], 0)

	def test_in_transit_supply_is_tracked_separately_from_factory_dispatch(self):
		from dms_erp.warehouse import inward_api

		item = self._item("INTRANSIT")
		po = self._po(item, 50, DISPATCH_DAYS - 10)
		inward_api.add_truck(
			supplier=self.supplier, item=item, boxes=20, purchase_order=po["id"], purchase_order_item=po["lines"][0]["id"]
		)
		order = self._order(item, 15)

		rows = delivery_risk.at_risk_sales_order_lines(as_of=_as_of())
		row = next(r for r in rows if r["salesOrder"] == order)
		self.assertEqual(row["coveredFrom"], "in_transit_delayed")
		self.assertTrue(row["atRisk"])
