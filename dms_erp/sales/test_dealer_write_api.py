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

	def test_create_dealer_rejects_the_old_name_of_a_renamed_dealer(self):
		dealer_api.create_dealer("DW Rename Co", group=GROUP)
		dealer_api.update_dealer("DW Rename Co", {"name": "DW Renamed Co"})
		with self.assertRaises(frappe.DuplicateEntryError):
			dealer_api.create_dealer("DW Rename Co", group=GROUP)

	def test_update_dealer_rejects_renaming_to_a_name_another_dealer_uses(self):
		dealer_api.create_dealer("DW Rename A", group=GROUP)
		dealer_api.create_dealer("DW Rename B", group=GROUP)
		with self.assertRaises(frappe.DuplicateEntryError):
			dealer_api.update_dealer("DW Rename B", {"name": "DW Rename A"})
		self.assertEqual(dealer_api.update_dealer("DW Rename B", {"name": "DW Rename B"})["name"], "DW Rename B")

	def test_update_dealer_changes_fields_but_never_classification(self):
		dealer_api.create_dealer("DW Update Co", group=GROUP)
		updated = dealer_api.update_dealer(
			"DW Update Co", {"dealerType": "Project", "creditLimit": 90000, "classification": "Master Dealer"}
		)
		self.assertEqual(updated["dealerType"], "Project")
		self.assertEqual(updated["creditLimit"], 90000)
		self.assertNotEqual(updated["classification"], "Master Dealer")

		self.assertTrue(dealer_api.update_dealer("DW Update Co", {"disabled": True})["disabled"])

	def test_create_dealer_normalizes_the_phone_number(self):
		dealer = dealer_api.create_dealer("DW Phone Co", group=GROUP, phone="+91 96202 04657")
		self.assertEqual(dealer["phone"], "9620204657")
		self.assertEqual(frappe.db.get_value("Customer", "DW Phone Co", "custom_phone"), "9620204657")

	def test_create_dealer_rejects_an_invalid_phone_number(self):
		with self.assertRaises(frappe.ValidationError):
			dealer_api.create_dealer("DW Bad Phone Co", group=GROUP, phone="12345")

	def test_update_dealer_normalizes_the_phone_number(self):
		dealer_api.create_dealer("DW Phone Update Co", group=GROUP)
		updated = dealer_api.update_dealer("DW Phone Update Co", {"phone": "09620204657"})
		self.assertEqual(updated["phone"], "9620204657")

	def test_create_dealer_defaults_to_price_visible_and_can_hide_it(self):
		default_dealer = dealer_api.create_dealer("DW Price Default Co", group=GROUP)
		self.assertTrue(default_dealer["priceVisible"])

		hidden_dealer = dealer_api.create_dealer("DW Price Hidden Co", group=GROUP, price_visible=False)
		self.assertFalse(hidden_dealer["priceVisible"])

	def test_update_dealer_toggles_price_visible(self):
		dealer_api.create_dealer("DW Price Toggle Co", group=GROUP)
		hidden = dealer_api.update_dealer("DW Price Toggle Co", {"priceVisible": False})
		self.assertFalse(hidden["priceVisible"])
		shown = dealer_api.update_dealer("DW Price Toggle Co", {"priceVisible": True})
		self.assertTrue(shown["priceVisible"])

	def test_write_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			dealer_api.create_dealer("DW Denied Co", group=GROUP)
		with self.assertRaises(frappe.PermissionError):
			dealer_api.update_dealer("DW Update Co", {"disabled": True})
