import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import api as catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing.setup import setup_pricing
from dms_erp.purchase.reorder_api import SAFETY_STOCK_BOXES, reorder_suggestions
from dms_erp.warehouse import allocation_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_item, make_supplier


class TestReorder(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Reorder Test Supplier")
		cls.bay = make_bay("REORDER-A-01", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def _suggestion_for(self, item_code):
		return next(s for s in reorder_suggestions() if s["productId"] == item_code)

	def test_low_stock_item_is_watch_with_positive_suggested_qty(self):
		item = make_item("REORDER-LOW", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="REORDER-LOW-B1", total_qty=20, lines=[{"bay": "REORDER-A-01", "qty": 20}], supplier=self.supplier
		)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["currentStock"], 20)
		self.assertGreater(suggestion["suggestedQty"], 0)
		self.assertEqual(suggestion["urgency"], "Watch")
		self.assertIn(f"Below {SAFETY_STOCK_BOXES}-box safety stock", suggestion["reasons"])

	def test_well_stocked_item_is_healthy_with_no_suggestion(self):
		item = make_item("REORDER-HEALTHY", "Vitrified")
		allocation_api.create_allocation(
			item=item, batch_no="REORDER-HEALTHY-B1", total_qty=500, lines=[{"bay": "REORDER-A-01", "qty": 500}], supplier=self.supplier
		)

		suggestion = self._suggestion_for(item)
		self.assertEqual(suggestion["currentStock"], 500)
		self.assertEqual(suggestion["suggestedQty"], 0)
		self.assertEqual(suggestion["urgency"], "Healthy")

	def test_non_reorderable_item_never_gets_a_suggestion(self):
		item = make_item("REORDER-PULLED", "Vitrified")
		catalog_api.update_product(item, {"status": "Pulled Back"})

		suggestion = self._suggestion_for(item)
		self.assertTrue(suggestion["nonReorderable"])
		self.assertEqual(suggestion["suggestedQty"], 0)
		self.assertEqual(suggestion["urgency"], "Healthy")
