import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.sales import dealer_api
from dms_erp.warehouse.test_fixtures import make_dealer


class TestDealerApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.dealer = make_dealer("Dealer Api Test Co")

	def test_get_dealer_returns_native_customer_fields(self):
		result = dealer_api.get_dealer(self.dealer)
		self.assertEqual(result["id"], self.dealer)
		self.assertEqual(result["name"], self.dealer)
		self.assertEqual(result["group"], "All Customer Groups")
		self.assertEqual(result["territory"], "All Territories")
		self.assertFalse(result["disabled"])

	def test_list_dealers_filters_by_search_and_excludes_disabled_by_default(self):
		results = dealer_api.list_dealers(search="Dealer Api Test")
		self.assertIn(self.dealer, [r["id"] for r in results])

		frappe.db.set_value("Customer", self.dealer, "disabled", 1)
		try:
			active = dealer_api.list_dealers(search="Dealer Api Test")
			self.assertNotIn(self.dealer, [r["id"] for r in active])

			disabled_only = dealer_api.list_dealers(search="Dealer Api Test", disabled=True)
			self.assertIn(self.dealer, [r["id"] for r in disabled_only])
		finally:
			frappe.db.set_value("Customer", self.dealer, "disabled", 0)
