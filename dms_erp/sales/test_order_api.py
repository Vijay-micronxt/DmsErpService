import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import series_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.dealer_classification import DEALER_CLASSIFICATION_MASTER
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, order_api, picking_api
from dms_erp.sales import utils as sales_utils
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
		if frappe.db.exists("Product Series", "Order Test Series"):
			frappe.delete_doc("Product Series", "Order Test Series", force=True, ignore_permissions=True)

	def _make_order(self, qty=10):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=qty, source="Phone")
		return order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": qty}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)

	def test_create_order_exposes_created_at_timestamp(self):
		# Sales Order.transaction_date is a plain Date field (no time-of-day) --
		# createdAt is the actual creation Datetime, for tables that need to show
		# time alongside date.
		order = self._make_order()
		self.assertIsNotNone(order["createdAt"])

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
		converted = inquiry_api.get_inquiry(inquiry["id"])
		self.assertEqual(converted["status"], "Converted to Order")
		self.assertEqual(converted["linkedSalesOrder"], order["id"])

	def test_create_order_uses_the_dealers_tiered_price_not_the_flat_dealer_list(self):
		series_api.create_series(
			series_name="Order Test Series",
			price_list_rates=[{"price_list": DEALER_CLASSIFICATION_MASTER, "rate": 700}],
		)
		item = make_item("ORDER-TIER-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_series_ref", "Order Test Series")
		master_dealer = make_dealer("Order Tier Master Dealer")
		frappe.db.set_value("Customer", master_dealer, "custom_dealer_classification", DEALER_CLASSIFICATION_MASTER)
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")

		inquiry = inquiry_api.create_inquiry(dealer=master_dealer, item=item, qty=10, source="Phone")
		order = order_api.create_order(
			dealer=master_dealer, lines=[{"item": item, "qty": 10}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)

		# Flat "Dealer" list price is 500; this dealer's Master Dealer tier rate is
		# 700 -- the order must be priced from the tier, not the flat list.
		self.assertEqual(order["lines"][0]["rate"], 700)

	def test_order_line_carries_the_items_weight(self):
		frappe.db.set_value("Item", self.item, "custom_weight_per_box_kg", 28)
		order = self._make_order(qty=10)
		self.assertEqual(order["lines"][0]["weightPerBoxKg"], 28)
		self.assertEqual(order["lines"][0]["totalWeightKg"], 280)

	def test_order_line_carries_the_items_pieces_and_sqft(self):
		frappe.db.set_value("Item", self.item, "custom_pieces_per_box", 4)
		frappe.db.set_value("Item", self.item, "custom_sqft_per_box", 15.5)
		order = self._make_order(qty=10)
		self.assertEqual(order["lines"][0]["piecesPerBox"], 4)
		self.assertEqual(order["lines"][0]["totalPieces"], 40)
		self.assertEqual(order["lines"][0]["sqftPerBox"], 15.5)
		self.assertEqual(order["lines"][0]["totalSqft"], 155)
		self.assertAlmostEqual(order["lines"][0]["sqmPerBox"], 1.44, places=2)
		self.assertAlmostEqual(order["lines"][0]["totalSqm"], 14.4, places=2)

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

	def test_create_order_auto_classifies_bulk_from_the_dealers_type(self):
		bulk_dealer = make_dealer("Order Bulk-Type Dealer")
		frappe.db.set_value("Customer", bulk_dealer, "custom_dealer_type", "Project")
		inquiry = inquiry_api.create_inquiry(dealer=bulk_dealer, item=self.item, qty=10, source="Phone")

		order = order_api.create_order(
			dealer=bulk_dealer, lines=[{"item": self.item, "qty": 10}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)
		self.assertEqual(order["channel"], "Project")

	def test_advance_order_stage_follows_forward_flow(self):
		order = self._make_order()

		updated = order_api.advance_order_stage(order["id"], "Picking")
		self.assertEqual(updated["stage"], "Picking")
		self.assertEqual(len(updated["history"]), 3)  # Created, Confirmed, Picking

		order_api.confirm_advance_payment(order["id"])
		updated = order_api.advance_order_stage(order["id"], "Ready to Dispatch")
		self.assertEqual(updated["stage"], "Ready to Dispatch")

	def test_advance_order_stage_refuses_ready_to_dispatch_without_advance_confirmed(self):
		order = self._make_order()
		order_api.advance_order_stage(order["id"], "Picking")
		with self.assertRaises(frappe.ValidationError):
			order_api.advance_order_stage(order["id"], "Ready to Dispatch")

	def test_confirm_advance_payment_unblocks_ready_to_dispatch(self):
		order = self._make_order()
		order_api.advance_order_stage(order["id"], "Picking")

		confirmed = order_api.confirm_advance_payment(order["id"])
		self.assertTrue(confirmed["advanceConfirmed"])

		updated = order_api.advance_order_stage(order["id"], "Ready to Dispatch")
		self.assertEqual(updated["stage"], "Ready to Dispatch")

	def test_confirm_advance_payment_requires_management_role(self):
		order = self._make_order()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			order_api.confirm_advance_payment(order["id"])

	def test_confirm_advance_payment_can_be_reversed(self):
		order = self._make_order()
		order_api.confirm_advance_payment(order["id"])
		reversed_order = order_api.confirm_advance_payment(order["id"], confirmed=False)
		self.assertFalse(reversed_order["advanceConfirmed"])

		order_api.advance_order_stage(order["id"], "Picking")
		with self.assertRaises(frappe.ValidationError):
			order_api.advance_order_stage(order["id"], "Ready to Dispatch")

	def test_advance_order_stage_rejects_skipping_ahead(self):
		order = self._make_order()
		with self.assertRaises(frappe.ValidationError):
			order_api.advance_order_stage(order["id"], "Dispatched")

	def test_advance_order_stage_allows_cancel_but_not_after_delivered(self):
		order = self._make_order()
		cancelled = order_api.advance_order_stage(order["id"], "Cancelled")
		self.assertEqual(cancelled["stage"], "Cancelled")

		order2 = self._make_order()
		order_api.confirm_advance_payment(order2["id"])
		for stage in ("Picking", "Ready to Dispatch", "Dispatched", "Delivered"):
			order_api.advance_order_stage(order2["id"], stage)
		with self.assertRaises(frappe.ValidationError):
			order_api.advance_order_stage(order2["id"], "Cancelled")

	def test_entering_picking_stage_creates_pick_tasks(self):
		order = self._make_order(qty=25)
		order_api.advance_order_stage(order["id"], "Picking")

		tasks = picking_api.list_pick_tasks(order["id"])["items"]
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

	def test_create_order_with_no_discount_passes_the_approved_rate_through_unrounded(self):
		# Regression guard: discount support must not introduce new rounding for
		# the (still overwhelmingly common) zero-discount line.
		order = self._make_order()
		self.assertEqual(order["lines"][0]["priceListRate"], 360)
		self.assertEqual(order["lines"][0]["discountPercentage"], 0)
		self.assertEqual(order["lines"][0]["rate"], 360)

	def test_create_order_applies_line_level_discount(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		order = order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": 10, "discount_percentage": 10}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)
		line = order["lines"][0]
		self.assertEqual(line["priceListRate"], 360)
		self.assertEqual(line["discountPercentage"], 10)
		self.assertEqual(line["rate"], 324)  # 360 * 0.9

	def test_create_order_rejects_an_out_of_range_discount(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		with self.assertRaises(frappe.ValidationError):
			order_api.create_order(
				dealer=self.dealer,
				lines=[{"item": self.item, "qty": 10, "discount_percentage": 150}],
				expected_dispatch="2026-09-01",
				inquiry=inquiry["id"],
			)

	def test_create_order_respects_a_per_line_delivery_date_override(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		order = order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": 10, "delivery_date": "2026-09-15"}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
		)
		self.assertEqual(order["lines"][0]["deliveryDate"], "2026-09-15")
		self.assertEqual(order["expectedDispatch"], "2026-09-01")

	def test_create_order_falls_back_to_the_order_level_delivery_date(self):
		order = self._make_order()
		self.assertEqual(order["lines"][0]["deliveryDate"], "2026-09-01")

	def _make_tax_template(self, name_suffix: str, rate: float = 18, is_default: bool = False) -> str:
		company = ensure_company()
		account = frappe.get_all("Account", filters={"company": company, "is_group": 0}, limit=1, pluck="name")
		if not account:
			self.skipTest("Test company has no Chart of Accounts to pick a leaf account from.")
		template_name = f"Order Test GST {name_suffix}"
		if frappe.db.exists("Sales Taxes and Charges Template", {"title": template_name, "company": company}):
			existing = frappe.db.get_value("Sales Taxes and Charges Template", {"title": template_name, "company": company}, "name")
			if is_default:
				existing_doc = frappe.get_doc("Sales Taxes and Charges Template", existing)
				existing_doc.is_default = 1
				existing_doc.save(ignore_permissions=True)
			return existing
		template = frappe.get_doc(
			{
				"doctype": "Sales Taxes and Charges Template",
				"title": template_name,
				"company": company,
				"is_default": 1 if is_default else 0,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": account[0],
						"description": template_name,
						"rate": rate,
					}
				],
			}
		)
		template.insert(ignore_permissions=True)
		return template.name

	def test_create_order_applies_a_tax_template_and_lets_erpnext_compute_the_total(self):
		template_name = self._make_tax_template("A", rate=18)
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		order = order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": 10}],
			expected_dispatch="2026-09-01",
			inquiry=inquiry["id"],
			taxes_and_charges=template_name,
		)
		self.assertEqual(order["taxesAndCharges"], template_name)
		self.assertEqual(len(order["taxes"]), 1)
		self.assertEqual(order["taxes"][0]["rate"], 18)
		# 10 boxes * 360/box = 3600 net; 18% of that is exactly what ERPNext's own
		# calculate_taxes_and_totals should have computed, not anything this app derived.
		self.assertEqual(order["netTotal"], 3600)
		self.assertEqual(order["totalTaxesAndCharges"], 648)
		self.assertEqual(order["total"], 4248)

	def test_create_order_without_a_tax_template_stays_untaxed(self):
		order = self._make_order()
		self.assertFalse(order["taxesAndCharges"])
		self.assertEqual(order["taxes"], [])
		self.assertEqual(order["total"], order["netTotal"])

	def test_create_order_without_a_tax_template_ignores_the_companys_default_template(self):
		"""Regression for the "hardcoded 18% for all customers" QA report: on a site
		with a default Sales Taxes and Charges Template configured, ERPNext's own
		validate()-time logic (Accounts Settings > "Add taxes from Taxes and Charges/
		Item Tax Template") would otherwise silently apply it to any new order whose
		taxes table is still empty -- regardless of what the caller asked for. An
		order created with no taxes_and_charges must stay genuinely untaxed even when
		a default exists, not just when the test company happens to have none."""
		self._make_tax_template("Default", rate=18, is_default=True)
		order = self._make_order()
		self.assertFalse(order["taxesAndCharges"])
		self.assertEqual(order["taxes"], [])
		self.assertEqual(order["totalTaxesAndCharges"], 0)
		self.assertEqual(order["total"], order["netTotal"])

	def test_list_tax_templates_surfaces_the_sites_own_configured_templates(self):
		template_name = self._make_tax_template("B", rate=12)
		templates = sales_utils.list_tax_templates()
		self.assertIn(template_name, [t["id"] for t in templates])
