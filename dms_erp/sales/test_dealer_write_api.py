import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.sales import dealer_api
from dms_erp.warehouse.test_fixtures import ensure_company

GROUP = "Dealer Write Test Group"


class TestDealerWriteApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		if not frappe.db.exists("Customer Group", GROUP):
			frappe.get_doc(
				{"doctype": "Customer Group", "customer_group_name": GROUP, "parent_customer_group": "All Customer Groups", "is_group": 0}
			).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_dealer_sets_fields_and_credit_limit(self):
		dealer = dealer_api.create_dealer("DW Create Co", group=GROUP, dealer_type="Bulk", credit_limit=125000)
		self.assertEqual(dealer["name"], "DW Create Co")
		self.assertEqual(dealer["group"], GROUP)
		self.assertEqual(dealer["dealerType"], "Bulk")
		self.assertEqual(dealer["creditLimit"], 125000)

	def test_create_dealer_rejects_duplicate_name_and_bad_type(self):
		dealer_api.create_dealer("DW Dup Co", group=GROUP)
		with self.assertRaises(frappe.DuplicateEntryError):
			dealer_api.create_dealer("DW Dup Co", group=GROUP)
		with self.assertRaises(frappe.ValidationError):
			dealer_api.create_dealer("DW Bad Type Co", group=GROUP, dealer_type="Wholesale")

	def test_update_dealer_changes_fields_but_never_classification(self):
		dealer_api.create_dealer("DW Update Co", group=GROUP)
		updated = dealer_api.update_dealer(
			"DW Update Co", {"dealerType": "Project", "creditLimit": 90000, "classification": "Master Dealer"}
		)
		self.assertEqual(updated["dealerType"], "Project")
		self.assertEqual(updated["creditLimit"], 90000)
		self.assertNotEqual(updated["classification"], "Master Dealer")

		self.assertTrue(dealer_api.update_dealer("DW Update Co", {"disabled": True})["disabled"])

	def test_write_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			dealer_api.create_dealer("DW Denied Co", group=GROUP)
		with self.assertRaises(frappe.PermissionError):
			dealer_api.update_dealer("DW Update Co", {"disabled": True})
