import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.sales import dealer_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer
from dms_erp.warehouse.utils import default_company


class TestDealerApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Dealer Api Test Co")

	def test_get_dealer_returns_native_customer_fields(self):
		result = dealer_api.get_dealer(self.dealer)
		self.assertEqual(result["id"], self.dealer)
		self.assertEqual(result["name"], self.dealer)
		self.assertEqual(result["group"], "All Customer Groups")
		self.assertEqual(result["territory"], "All Territories")
		self.assertFalse(result["disabled"])

	def test_credit_limit_reads_from_the_child_table_not_a_flat_column(self):
		# Customer.credit_limit is a Customer Credit Limit child row keyed by
		# company on modern ERPNext, not a flat column — see dealer_api.py's
		# module docstring for why.
		doc = frappe.get_doc("Customer", self.dealer)
		doc.append("credit_limits", {"company": default_company(), "credit_limit": 250000})
		doc.save(ignore_permissions=True)

		self.assertEqual(dealer_api.get_dealer(self.dealer)["creditLimit"], 250000)

		listed = next(r for r in dealer_api.list_dealers(search="Dealer Api Test") if r["id"] == self.dealer)
		self.assertEqual(listed["creditLimit"], 250000)

	def test_dealer_with_no_credit_limit_set_defaults_to_zero(self):
		bare_dealer = make_dealer("Dealer Api No Credit Limit Co")
		self.assertEqual(dealer_api.get_dealer(bare_dealer)["creditLimit"], 0)

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
