import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog import series_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.dealer_classification import DEALER_CLASSIFICATION_MASTER
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, order_api, picking_api
from dms_erp.warehouse import allocation_api
from dms_erp.sales import utils as sales_utils
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_dealer, make_item, make_supplier


# Relative to today: fixed dates rot into the past, and ERPNext then refuses the order
# ("Expected Delivery Date should be after Sales Order Date").
DISPATCH = str(add_days(today(), 30))
LINE_DELIVERY = str(add_days(today(), 44))


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

	def _stock(self, item, qty, bay=None):
		"""Real on-hand stock so a Delivery Note can actually post against it (negative
		stock is refused site-wide, same as a real warehouse). Returns the Warehouse
		doc's own name -- what Pick Task.suggested_bay / Delivery Note.warehouse hold --
		not the human bay code create_allocation's own `lines[].bay` takes."""
		bay = bay or make_bay(f"{item}-DN-BAY", categories=["Vitrified"])
		allocation_api.create_allocation(
			item=item, batch_no=f"{item}-DN-B1", total_qty=qty, lines=[{"bay": bay["code"], "qty": qty}], supplier=self.supplier
		)
		return bay["id"]

	def _dispatch(self, order_id):
		"""Walks an order from Picking through to Dispatched, actually marking its one
		Pick Task Picked -- the real readiness gate _assert_lines_ready_for_delivery
		checks (stock must already exist via self._stock first, or the Delivery Note
		this posts will be refused for insufficient stock, same as production)."""
		order_api.advance_order_stage(order_id, "Picking")
		task = picking_api.list_pick_tasks(order_id)["items"][0]
		picking_api.auto_allocate(task["id"])
		picking_api.patch_task(task["id"], {"picker": "Administrator", "status": "Picked"})
		order_api.confirm_advance_payment(order_id)
		order_api.advance_order_stage(order_id, "Ready to Dispatch")
		return order_api.advance_order_stage(order_id, "Dispatched")

	def _make_order(self, qty=10):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=qty, source="Phone")
		return order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": qty}], expected_dispatch=DISPATCH, inquiry=inquiry["id"]
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
			dealer=self.dealer, lines=[{"item": self.item, "qty": 40}], expected_dispatch=DISPATCH, inquiry=inquiry["id"]
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
			dealer=master_dealer, lines=[{"item": item, "qty": 10}], expected_dispatch=DISPATCH, inquiry=inquiry["id"]
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
			dealer=self.dealer, lines=[{"item": self.item, "qty": 500}], expected_dispatch=DISPATCH, inquiry=inquiry["id"], channel="Bulk"
		)
		self.assertEqual(order["channel"], "Bulk")

	def test_create_order_rejects_invalid_channel(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		with self.assertRaises(frappe.ValidationError):
			order_api.create_order(
				dealer=self.dealer, lines=[{"item": self.item, "qty": 10}], expected_dispatch=DISPATCH, inquiry=inquiry["id"], channel="Wholesale"
			)

	def test_create_order_channel_override_by_an_unauthorized_user_is_queued_not_applied(self):
		email = "order.sales.only@pacific.test"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Sales",
				"send_welcome_email": 0,
				"roles": [{"role": "DMS Sales"}],
			}
		).insert(ignore_permissions=True)

		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		before = frappe.db.count("Sales Order")

		frappe.set_user(email)
		result = order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": 10}], expected_dispatch=DISPATCH, inquiry=inquiry["id"], channel="Bulk"
		)

		self.assertTrue(result["approvalRequired"])
		self.assertEqual(result["approval"]["triggerType"], "Channel Override")
		self.assertEqual(frappe.db.count("Sales Order"), before)

	def test_create_order_auto_classifies_bulk_from_the_dealers_type(self):
		bulk_dealer = make_dealer("Order Bulk-Type Dealer")
		frappe.db.set_value("Customer", bulk_dealer, "custom_dealer_type", "Project")
		inquiry = inquiry_api.create_inquiry(dealer=bulk_dealer, item=self.item, qty=10, source="Phone")

		order = order_api.create_order(
			dealer=bulk_dealer, lines=[{"item": self.item, "qty": 10}], expected_dispatch=DISPATCH, inquiry=inquiry["id"]
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
		self._stock(self.item, 10)
		self._dispatch(order2["id"])
		order_api.advance_order_stage(order2["id"], "Delivered")
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

	def test_dispatch_is_refused_until_every_line_is_marked_picked(self):
		item = make_item("ORDER-DISPATCH-UNPICKED", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		self._stock(item, 10)

		order = order_api.create_order(dealer=self.dealer, lines=[{"item": item, "qty": 10}], expected_dispatch=DISPATCH)
		order_api.confirm_advance_payment(order["id"])
		order_api.advance_order_stage(order["id"], "Picking")
		order_api.advance_order_stage(order["id"], "Ready to Dispatch")

		with self.assertRaises(frappe.ValidationError):
			order_api.advance_order_stage(order["id"], "Dispatched")
		# The stage must not have half-advanced.
		self.assertEqual(order_api.get_order(order["id"])["stage"], "Ready to Dispatch")

		task = picking_api.list_pick_tasks(order["id"])["items"][0]
		picking_api.auto_allocate(task["id"])
		picking_api.patch_task(task["id"], {"picker": "Administrator", "status": "Picked"})
		dispatched = order_api.advance_order_stage(order["id"], "Dispatched")
		self.assertEqual(dispatched["stage"], "Dispatched")

	def test_dispatch_posts_a_real_delivery_note_against_the_picked_bay_and_batch(self):
		item = make_item("ORDER-DN-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		bay = self._stock(item, 10)
		from dms_erp.warehouse.utils import total_stock_for_item

		self.assertEqual(total_stock_for_item(item), 10)

		order = order_api.create_order(dealer=self.dealer, lines=[{"item": item, "qty": 10}], expected_dispatch=DISPATCH)
		order_api.advance_order_stage(order["id"], "Picking")
		task = picking_api.list_pick_tasks(order["id"])["items"][0]
		picking_api.auto_allocate(task["id"])
		picking_api.patch_task(task["id"], {"picker": "Administrator", "status": "Picked"})
		order_api.confirm_advance_payment(order["id"])
		order_api.advance_order_stage(order["id"], "Ready to Dispatch")

		dispatched = order_api.advance_order_stage(order["id"], "Dispatched")
		self.assertEqual(len(dispatched["deliveryNotes"]), 1)
		dn = dispatched["deliveryNotes"][0]
		self.assertEqual(dn["lines"], [{"itemCode": item, "qty": 10, "warehouse": bay, "batchNo": f"{item}-DN-B1"}])
		self.assertTrue(frappe.db.get_value("Delivery Note", dn["id"], "docstatus") == 1)

		# The real stock effect: on-hand drops by what was actually dispatched.
		self.assertEqual(total_stock_for_item(item), 0)

		# Dispatching the same order a second time (e.g. a retry) must never double-post.
		with self.assertRaises(frappe.ValidationError):
			order_api.advance_order_stage(order["id"], "Dispatched")

	def test_create_sales_invoice_carries_the_orders_channel(self):
		# BRD C.4.3 -- the retail/bulk tag follows the order onto its invoice.
		bulk_order = order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": 3}], expected_dispatch=DISPATCH, channel="Bulk"
		)
		frappe.db.set_value("Sales Order", bulk_order["id"], "custom_fulfillment_stage", "Dispatched")
		invoice = order_api.create_sales_invoice(bulk_order["id"])
		self.assertEqual(invoice["channel"], "Bulk")
		# What the frontend actually re-fetches (createSalesInvoice follows with get_order).
		self.assertEqual(order_api.get_order(bulk_order["id"])["invoices"][0]["channel"], "Bulk")

		retail_order = self._make_order()
		frappe.db.set_value("Sales Order", retail_order["id"], "custom_fulfillment_stage", "Dispatched")
		invoice = order_api.create_sales_invoice(retail_order["id"])
		self.assertEqual(invoice["channel"], "Retail")

	def test_write_requires_sales_or_management_role(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=1, source="Phone")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			order_api.create_order(dealer=self.dealer, lines=[{"item": self.item, "qty": 1}], expected_dispatch=DISPATCH, inquiry=inquiry["id"])

	def test_list_orders_is_paginated_and_searchable(self):
		dealer = make_dealer("Order Pagination Dealer")
		item = make_item("ORDER-PAGINATION-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")

		created = []
		for _ in range(3):
			inquiry = inquiry_api.create_inquiry(dealer=dealer, item=item, qty=10, source="Phone")
			created.append(
				order_api.create_order(dealer=dealer, lines=[{"item": item, "qty": 10}], expected_dispatch=DISPATCH, inquiry=inquiry["id"])
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
			expected_dispatch=DISPATCH,
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
				expected_dispatch=DISPATCH,
				inquiry=inquiry["id"],
			)

	def test_create_order_respects_a_per_line_delivery_date_override(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		order = order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": 10, "delivery_date": LINE_DELIVERY}],
			expected_dispatch=DISPATCH,
			inquiry=inquiry["id"],
		)
		self.assertEqual(order["lines"][0]["deliveryDate"], LINE_DELIVERY)
		# ERPNext's own SalesOrder.validate moves the header delivery date to the latest
		# line's date, so a later per-line date becomes the order's expected dispatch.
		self.assertEqual(order["expectedDispatch"], LINE_DELIVERY)

	def test_create_order_falls_back_to_the_order_level_delivery_date(self):
		order = self._make_order()
		self.assertEqual(order["lines"][0]["deliveryDate"], DISPATCH)

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
			expected_dispatch=DISPATCH,
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

	def test_same_items_and_quantities_again_are_flagged_as_a_possible_duplicate(self):
		# BRD C.2.4: same dealer, same material and quantity, different PO number.
		from dms_erp.warehouse.test_fixtures import make_dealer

		dealer = make_dealer("Duplicate PO Test Dealer")
		lines = [{"item": self.item, "qty": 7}]
		first = order_api.create_order(dealer=dealer, lines=lines, expected_dispatch=DISPATCH, customer_po="PO-A")
		self.assertEqual(first["possibleDuplicates"], [])

		second = order_api.create_order(dealer=dealer, lines=lines, expected_dispatch=DISPATCH, customer_po="PO-B")
		self.assertEqual(second["possibleDuplicates"], [first["id"]])
		self.assertTrue(any("Possible duplicate" in (e.get("note") or "") for e in second["history"]))

		# A different quantity is a different order, not a duplicate.
		third = order_api.create_order(dealer=dealer, lines=[{"item": self.item, "qty": 8}], expected_dispatch=DISPATCH)
		self.assertEqual(third["possibleDuplicates"], [])

		# A cancelled order no longer counts.
		order_api.advance_order_stage(first["id"], "Cancelled")
		order_api.advance_order_stage(second["id"], "Cancelled")
		fourth = order_api.create_order(dealer=dealer, lines=lines, expected_dispatch=DISPATCH)
		self.assertEqual(fourth["possibleDuplicates"], [])

	def _make_uom_item(self):
		item = make_item("ORDER-UOM-TEST-ITEM", "Vitrified")
		frappe.db.set_value(
			"Item",
			item,
			{"custom_pieces_per_box": 4, "custom_sqft_per_box": 15.5, "custom_weight_per_box_kg": 28},
		)
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		return item

	def test_order_line_entered_in_sqft_is_converted_to_boxes(self):
		item = self._make_uom_item()
		order = order_api.create_order(
			dealer=self.dealer, lines=[{"item": item, "qty": 50, "uom": "Sqft"}], expected_dispatch=DISPATCH
		)
		line = order["lines"][0]
		self.assertEqual(line["qty"], 4)  # ceil(50 / 15.5) = 4 boxes
		self.assertEqual(line["enteredQty"], 50)
		self.assertEqual(line["enteredUom"], "Sqft")
		self.assertEqual(line["amount"], 4 * 360)

	def test_order_line_left_in_box_carries_no_entered_fields(self):
		order = self._make_order(qty=6)
		line = order["lines"][0]
		self.assertEqual(line["qty"], 6)
		self.assertIsNone(line["enteredQty"])
		self.assertIsNone(line["enteredUom"])

	def test_order_line_uom_without_a_conversion_factor_is_refused(self):
		item = make_item("ORDER-UOM-NO-FACTOR-ITEM", "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=360, reason="Launch")
		with self.assertRaises(frappe.ValidationError):
			order_api.create_order(
				dealer=self.dealer, lines=[{"item": item, "qty": 10, "uom": "Pcs"}], expected_dispatch=DISPATCH
			)

	def test_uom_conversion_happens_before_channel_classification(self):
		"""A line entered in Sqft must be converted to boxes before auto_classify_channel
		ever compares qty against the item's bulk_qty_threshold: 2000 (raw sqft) would
		wrongly trip a 500-box Bulk threshold, but the real box-equivalent (130) does not."""
		item = self._make_uom_item()
		frappe.db.set_value("Item", item, "custom_bulk_qty_threshold", 500)
		order = order_api.create_order(
			dealer=self.dealer, lines=[{"item": item, "qty": 2000, "uom": "Sqft"}], expected_dispatch=DISPATCH
		)
		self.assertEqual(order["lines"][0]["qty"], 130)  # ceil(2000 / 15.5)
		self.assertEqual(order["channel"], "Retail")  # 130 boxes stays under the 500-box threshold
