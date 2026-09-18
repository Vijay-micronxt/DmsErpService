import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import series_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.warehouse.test_fixtures import make_supplier


class TestSeriesApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup_catalog()
		cls.supplier = make_supplier("Series Test Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in ("Series Test Marbello", "Series Test Granito"):
			if frappe.db.exists("Series", name):
				frappe.delete_doc("Series", name, force=True, ignore_permissions=True)

	def test_create_series_and_get(self):
		created = series_api.create_series(
			series_name="Series Test Marbello",
			supplier=self.supplier,
			size="600x1200mm",
			thickness="9mm",
			finish="Glossy",
			pieces_per_box=2,
			sqft_per_box=15.5,
			weight_per_box_kg=28,
			bulk_qty_threshold=200,
			retail_qty_threshold=50,
		)
		self.assertEqual(created["id"], "Series Test Marbello")
		self.assertEqual(created["finish"], "Glossy")
		self.assertEqual(created["bulkQtyThreshold"], 200)

		fetched = series_api.get_series("Series Test Marbello")
		self.assertEqual(fetched["sqftPerBox"], 15.5)

	def test_list_series_filters_by_search(self):
		series_api.create_series(series_name="Series Test Marbello")
		series_api.create_series(series_name="Series Test Granito")

		results = series_api.list_series(search="Marbello")
		self.assertEqual([r["id"] for r in results["items"]], ["Series Test Marbello"])

	def test_update_series_patches_thresholds_and_rates(self):
		series_api.create_series(series_name="Series Test Marbello", bulk_qty_threshold=100)

		updated = series_api.update_series(
			"Series Test Marbello",
			{"bulkQtyThreshold": 250, "priceListRates": [{"price_list": "Standard Selling", "rate": 450}]},
		)
		self.assertEqual(updated["bulkQtyThreshold"], 250)
		self.assertEqual(updated["priceListRates"], [{"priceList": "Standard Selling", "rate": 450}])

	def test_create_series_requires_purchase_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			series_api.create_series(series_name="Series Test Marbello")
