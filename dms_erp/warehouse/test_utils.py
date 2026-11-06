import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.sales import order_api
from dms_erp.sales.setup import setup_sales
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_bay,
	make_dealer,
	make_dealer_addresses,
	make_item,
	make_supplier,
)
from dms_erp.warehouse.utils import (
	available_to_promise,
	excluded_stock_for_item,
	reserved_qty_for_item,
	sellable_stock_for_item,
	total_stock_for_item,
)


class TestSellableStockForItem(FrappeTestCase):
	"""Client finding "stock accuracy" — a dealer-facing available count must exclude
	damage/insurance-claim and display-bay stock, while total_stock_for_item (the
	staff-facing figure) stays the genuine physical total."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.item = make_item("SELLABLE-STOCK-TEST-ITEM", "Vitrified")
		cls.supplier = make_supplier("Sellable Stock Test Supplier")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 400, 25, "2026-08-01")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _stock(self, bay, qty):
		allocation_api.create_allocation(
			item=self.item,
			batch_no=f"SELLABLE-{bay['code']}-{qty}",
			total_qty=qty,
			lines=[{"bay": bay["code"], "qty": qty}],
			supplier=self.supplier,
		)

	def test_excludes_damage_and_display_bays_but_not_main_or_buffer(self):
		main_bay = make_bay("SELLABLE-MAIN-01", bay_type="main", categories=["Vitrified"])
		buffer_bay = make_bay("SELLABLE-BUF-01", bay_type="buffer", categories=["Vitrified"])
		damage_bay = make_bay("SELLABLE-DMG-01", bay_type="damage", categories=["Vitrified"])
		display_bay = make_bay("SELLABLE-DISP-01", bay_type="display", categories=["Vitrified"])

		self._stock(main_bay, 10)
		self._stock(buffer_bay, 5)
		self._stock(damage_bay, 2)
		self._stock(display_bay, 1)

		self.assertEqual(total_stock_for_item(self.item), 18)
		self.assertEqual(sellable_stock_for_item(self.item), 15)  # main + buffer only

	def test_excluded_stock_breakdown_groups_by_bay_type(self):
		damage_bay = make_bay("SELLABLE-DMG-02", bay_type="damage", categories=["Vitrified"])
		display_bay = make_bay("SELLABLE-DISP-02", bay_type="display", categories=["Vitrified"])
		self._stock(damage_bay, 3)
		self._stock(display_bay, 4)

		rows = {r["bayType"]: r["boxes"] for r in excluded_stock_for_item(self.item)}
		self.assertEqual(rows.get("damage"), 3)
		self.assertEqual(rows.get("display"), 4)

	def test_no_excluded_stock_is_an_empty_list(self):
		item = make_item("SELLABLE-STOCK-NO-EXCLUDED", "Vitrified")
		main_bay = make_bay("SELLABLE-MAIN-02", bay_type="main", categories=["Vitrified"])
		allocation_api.create_allocation(
			item=item, batch_no="SELLABLE-NO-EXCLUDED-B1", total_qty=5, lines=[{"bay": main_bay["code"], "qty": 5}], supplier=self.supplier
		)
		self.assertEqual(excluded_stock_for_item(item), [])
		self.assertEqual(sellable_stock_for_item(item), total_stock_for_item(item))


class TestAvailableToPromise(FrappeTestCase):
	"""BRD C.3.1 -- "stock is reserved on confirmation" had never actually reduced
	what another dealer saw as available. available_to_promise (and the
	order/quotation creation checks built on it) close that gap."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		setup_sales()
		cls.item = make_item("ATP-TEST-ITEM", "Vitrified")
		cls.supplier = make_supplier("ATP Test Supplier")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")
		cls.dealer = make_dealer("ATP Test Dealer")
		cls.billing_address, cls.shipping_address = make_dealer_addresses(cls.dealer)
		main_bay = make_bay("ATP-MAIN-01", bay_type="main", categories=["Vitrified"])
		allocation_api.create_allocation(
			item=cls.item, batch_no="ATP-BATCH-1", total_qty=50, lines=[{"bay": main_bay["code"], "qty": 50}], supplier=cls.supplier
		)

	def _order(self, qty) -> str:
		return order_api._create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": qty}],
			expected_dispatch=frappe.utils.add_days(frappe.utils.today(), 30),
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
		)["id"]

	def test_confirming_an_order_reduces_what_another_dealer_can_still_order(self):
		self.assertEqual(available_to_promise(self.item), 50)
		self._order(30)
		self.assertEqual(reserved_qty_for_item(self.item), 30)
		self.assertEqual(available_to_promise(self.item), 20)

	def test_a_second_order_over_the_remaining_qty_is_rejected(self):
		self._order(30)  # leaves 20 of the 50 free
		with self.assertRaises(frappe.ValidationError):
			self._order(21)
		# Exactly the remaining qty still goes through.
		self._order(20)
		self.assertEqual(available_to_promise(self.item), 0)

	def test_cancelling_an_order_frees_the_qty_back_up(self):
		order = self._order(10)
		self.assertEqual(reserved_qty_for_item(self.item), 10)
		frappe.db.set_value("Sales Order", order, "custom_fulfillment_stage", "Cancelled")
		self.assertEqual(reserved_qty_for_item(self.item), 0)
		self.assertEqual(available_to_promise(self.item), 50)

	def test_dispatching_an_order_also_stops_it_counting_as_reserved(self):
		# Dispatched means the stock has actually left -- already reflected in the
		# physical count, so it must not also still count as "reserved" on top of
		# that (double-counting the same boxes as gone twice over).
		order = self._order(15)
		frappe.db.set_value("Sales Order", order, "custom_fulfillment_stage", "Dispatched")
		self.assertEqual(reserved_qty_for_item(self.item), 0)

	def test_damaged_stock_line_skips_the_check_entirely(self):
		# Draws from the Damages Bay, a pool available_to_promise doesn't touch --
		# the item-wide check must not apply to it at all.
		order_api._create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": 500, "damaged_stock": True}],
			expected_dispatch=frappe.utils.add_days(frappe.utils.today(), 30),
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
		)

	def test_an_item_with_no_stock_at_all_is_still_orderable(self):
		# Client decision, 6 Nov 2026: a genuine backorder (never purchased, 0
		# stock) is not a double-booking -- it stays orderable exactly as before,
		# same as an Inquiry can already be raised for an out-of-stock item. The
		# check above only ever fires once an item has real stock to fight over.
		never_stocked = make_item("ATP-NEVER-STOCKED", "Vitrified")
		pricing_api.ensure_price_record(never_stocked, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=never_stocked, final_price=360, reason="Launch")
		self.assertEqual(available_to_promise(never_stocked), 0)
		order_api._create_order(
			dealer=self.dealer,
			lines=[{"item": never_stocked, "qty": 25}],
			expected_dispatch=frappe.utils.add_days(frappe.utils.today(), 30),
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
		)
