import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase import po_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.reports import delivery_risk as delivery_risk_report
from dms_erp.sales import order_api
from dms_erp.sales.setup import setup_sales
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_item,
	make_supplier,
)

# Sales Order's delivery_date must be strictly after today, so it can never be
# dated into the past the way a PO's expected_ready_date can (test_purchase_
# reports.py already creates one with add_days(today(), -3) directly). This
# report has no `as_of` param (matching every other report's convention -- it
# always evaluates "now"), so every order here uses the smallest valid future
# offset (+1 day) and a generous delivery_risk_eta_buffer_days makes that count
# as imminent, rather than back-dating anything.
ETA_BUFFER_DAYS = 5


class TestDeliveryRiskReport(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_purchase()
		setup_sales()
		cls.supplier_a = make_supplier("Delivery Risk Report Supplier A")
		cls.supplier_b = make_supplier("Delivery Risk Report Supplier B")
		cls.dealer_a = make_dealer("Delivery Risk Report Dealer A")
		cls.dealer_b = make_dealer("Delivery Risk Report Dealer B")

	def setUp(self):
		frappe.db.set_single_value("DMS Purchase Settings", "po_delay_grace_days", 0)
		frappe.db.set_single_value("DMS Purchase Settings", "delivery_risk_eta_buffer_days", ETA_BUFFER_DAYS)

	def tearDown(self):
		frappe.db.set_single_value("DMS Purchase Settings", "po_delay_grace_days", 0)
		frappe.db.set_single_value("DMS Purchase Settings", "delivery_risk_eta_buffer_days", 0)

	def _item(self, suffix: str, supplier: str) -> str:
		item = make_item(f"DELIVRISKRPT-{suffix}", "Vitrified")
		pricing_api.ensure_price_record(item, supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		return item

	def _delayed_order(self, item: str, supplier: str, dealer: str, qty: float = 20) -> str:
		"""One already-overdue PO and one open order due tomorrow (inside the
		configured ETA buffer) -- always produces an at-risk row."""
		po_api.create_purchase_order(item=item, ordered_qty=qty + 10, expected_ready_date=add_days(today(), -10), supplier=supplier)
		return order_api.create_order(dealer=dealer, lines=[{"item": item, "qty": qty}], expected_dispatch=add_days(today(), 1))["id"]

	def test_filters_by_item_customer_and_supplier(self):
		item_a = self._item("A", self.supplier_a)
		item_b = self._item("B", self.supplier_b)
		order_a = self._delayed_order(item_a, self.supplier_a, self.dealer_a)
		order_b = self._delayed_order(item_b, self.supplier_b, self.dealer_b)

		by_item = delivery_risk_report.delivery_risk_window(item=item_a)
		self.assertTrue(all(r["item"] == item_a for r in by_item["rows"]))
		self.assertIn(order_a, [r["salesOrder"] for r in by_item["rows"]])
		self.assertNotIn(order_b, [r["salesOrder"] for r in by_item["rows"]])

		by_customer = delivery_risk_report.delivery_risk_window(customer=self.dealer_b)
		self.assertTrue(all(r["customer"] == self.dealer_b for r in by_customer["rows"]))
		self.assertIn(order_b, [r["salesOrder"] for r in by_customer["rows"]])
		self.assertNotIn(order_a, [r["salesOrder"] for r in by_customer["rows"]])

		by_supplier = delivery_risk_report.delivery_risk_window(supplier=self.supplier_a)
		self.assertIn(order_a, [r["salesOrder"] for r in by_supplier["rows"]])
		self.assertNotIn(order_b, [r["salesOrder"] for r in by_supplier["rows"]])

	def test_summary_counts_and_rollups(self):
		item_a = self._item("C", self.supplier_a)
		item_b = self._item("D", self.supplier_b)
		order_a = self._delayed_order(item_a, self.supplier_a, self.dealer_a)
		order_b = self._delayed_order(item_b, self.supplier_b, self.dealer_a)  # same dealer, different item/supplier

		result = delivery_risk_report.delivery_risk_window(customer=self.dealer_a)
		rows = [r for r in result["rows"] if r["salesOrder"] in (order_a, order_b)]
		self.assertEqual(len(rows), 2)
		self.assertTrue(all(r["atRisk"] for r in rows))
		self.assertGreaterEqual(result["summary"]["atRiskCount"], 2)
		self.assertGreaterEqual(result["summary"]["byCustomer"].get(self.dealer_a, 0), 2)
		self.assertGreaterEqual(result["summary"]["byItem"].get(item_a, 0), 1)
		self.assertGreaterEqual(result["summary"]["byItem"].get(item_b, 0), 1)
		self.assertGreaterEqual(result["summary"]["bySupplier"].get(self.supplier_a, 0), 1)
		self.assertGreaterEqual(result["summary"]["bySupplier"].get(self.supplier_b, 0), 1)

	def test_not_at_risk_rows_are_included_but_dont_count_toward_at_risk_rollups(self):
		"""The window shows the full supply picture, not just at-risk lines -- an
		on-track item still gets a row, just never counted in the at-risk rollups."""
		item = self._item("ONTRACK", self.supplier_a)
		po_api.create_purchase_order(item=item, ordered_qty=50, expected_ready_date=add_days(today(), 10), supplier=self.supplier_a)
		order = order_api.create_order(dealer=self.dealer_a, lines=[{"item": item, "qty": 20}], expected_dispatch=add_days(today(), 1))[
			"id"
		]

		result = delivery_risk_report.delivery_risk_window(item=item)
		row = next(r for r in result["rows"] if r["salesOrder"] == order)
		self.assertFalse(row["atRisk"])
		self.assertEqual(result["summary"]["byItem"].get(item, 0), 0)
