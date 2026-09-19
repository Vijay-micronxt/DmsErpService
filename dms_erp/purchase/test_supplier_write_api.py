import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.purchase import supplier_api


class TestSupplierWriteApi(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_supplier_sets_fields(self):
		supplier = supplier_api.create_supplier("SW Create Co", country="India", latitude=22.8, longitude=70.8)
		self.assertEqual(supplier["name"], "SW Create Co")
		self.assertEqual(supplier["country"], "India")
		self.assertEqual(supplier["latitude"], 22.8)

	def test_create_supplier_rejects_duplicate_name(self):
		supplier_api.create_supplier("SW Dup Co")
		with self.assertRaises(frappe.DuplicateEntryError):
			supplier_api.create_supplier("SW Dup Co")

	def test_create_supplier_rejects_the_old_name_of_a_renamed_supplier(self):
		supplier_api.create_supplier("SW Rename Co")
		supplier_api.update_supplier("SW Rename Co", {"name": "SW Renamed Again Co"})
		with self.assertRaises(frappe.DuplicateEntryError):
			supplier_api.create_supplier("SW Rename Co")

	def test_update_supplier_rejects_renaming_to_a_name_another_supplier_uses(self):
		supplier_api.create_supplier("SW Rename A")
		supplier_api.create_supplier("SW Rename B")
		with self.assertRaises(frappe.DuplicateEntryError):
			supplier_api.update_supplier("SW Rename B", {"name": "SW Rename A"})
		self.assertEqual(supplier_api.update_supplier("SW Rename B", {"name": "SW Rename B"})["name"], "SW Rename B")

	def test_update_supplier_changes_fields_and_disables(self):
		supplier_api.create_supplier("SW Update Co")
		updated = supplier_api.update_supplier("SW Update Co", {"name": "SW Renamed Co", "latitude": 21.5, "disabled": True})
		self.assertEqual(updated["name"], "SW Renamed Co")
		self.assertEqual(updated["latitude"], 21.5)
		self.assertTrue(updated["disabled"])

	def test_write_requires_purchase_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			supplier_api.create_supplier("SW Denied Co")
		with self.assertRaises(frappe.PermissionError):
			supplier_api.update_supplier("SW Update Co", {"disabled": True})
