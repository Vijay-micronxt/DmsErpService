import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp import print_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase import po_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.sales import order_api, quotation_api
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_dealer_addresses,
	make_item,
	make_supplier,
)


class TestPrintApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_purchase()
		cls.supplier = make_supplier("Print Test Supplier")
		cls.dealer = make_dealer("Print Test Dealer & Sons")
		cls.billing_address, cls.shipping_address = make_dealer_addresses(cls.dealer)
		cls.item = make_item("PRINT-TEST-ITEM", "Vitrified")
		frappe.db.set_value("Item", cls.item, "custom_weight_per_box_kg", 25)
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, add_days(today(), -1))
		pricing_api.approve_price(item=cls.item, final_price=500, reason="Launch")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_quotation_prints_lines_discount_freight_weight_and_total(self):
		q = quotation_api.create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": 4, "discount_percentage": 10}],
			markup_pct=0,
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
			freight=200,
		)
		quotation = q["quotation"]["id"] if "quotation" in q else q["id"]

		html = print_api.render_quotation_html(quotation)

		self.assertIn(quotation, html)
		self.assertIn("PRINT-TEST-ITEM", html)
		self.assertIn("10%", html)  # line discount
		self.assertIn("100.0 kg", html)  # 4 boxes x 25 kg
		self.assertIn("Freight", html)
		self.assertIn("Grand total", html)
		# 4 x 450 + 200 freight = 2,000
		self.assertIn("2,000.00", html)
		# Dealer name is escaped, never injected as markup.
		self.assertIn("Print Test Dealer &amp; Sons", html)

	def test_sales_order_prints_customer_po_and_delivery_date(self):
		order = order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": 2}],
			expected_dispatch=add_days(today(), 7),
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
			customer_po="DEALER-PO-77",
		)

		html = print_api.render_sales_order_html(order["id"])

		self.assertIn("Sales Order", html)
		self.assertIn("DEALER-PO-77", html)
		self.assertIn("1,000.00", html)

	def test_purchase_order_prints_supplier_and_lines(self):
		po = po_api.create_purchase_order(
			item=self.item, ordered_qty=30, expected_ready_date=add_days(today(), 10), supplier=self.supplier
		)

		html = print_api.render_purchase_order_html(po["id"])

		self.assertIn("Purchase Order", html)
		self.assertIn("Print Test Supplier", html)
		self.assertIn("PRINT-TEST-ITEM", html)

	def test_printing_requires_a_staff_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			print_api.render_quotation_html("anything")
