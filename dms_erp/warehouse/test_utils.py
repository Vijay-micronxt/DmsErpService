import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_bay,
	make_item,
	make_supplier,
)
from dms_erp.warehouse.utils import (
	excluded_stock_for_item,
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
