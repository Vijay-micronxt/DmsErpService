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

	def test_create_and_update_supplier_set_insurance_holder(self):
		created = supplier_api.create_supplier("SW Insurance Co", insurance_holder="HDFC Ergo — POL-4471")
		self.assertEqual(created["insuranceHolder"], "HDFC Ergo — POL-4471")

		updated = supplier_api.update_supplier("SW Insurance Co", {"insuranceHolder": "ICICI Lombard — POL-9981"})
		self.assertEqual(updated["insuranceHolder"], "ICICI Lombard — POL-9981")

	def test_create_and_update_supplier_set_address_and_contact_person(self):
		created = supplier_api.create_supplier(
			"SW Address Co",
			address="Plot 12, GIDC Industrial Estate, Morbi, Gujarat",
			contact_person="Rajesh Patel — 98765 43210",
		)
		self.assertEqual(created["address"], "Plot 12, GIDC Industrial Estate, Morbi, Gujarat")
		self.assertEqual(created["contactPerson"], "Rajesh Patel — 98765 43210")

		updated = supplier_api.update_supplier(
			"SW Address Co",
			{"address": "Plot 45, Lalpar Road, Morbi, Gujarat", "contactPerson": "Suresh Shah — 91234 56789"},
		)
		self.assertEqual(updated["address"], "Plot 45, Lalpar Road, Morbi, Gujarat")
		self.assertEqual(updated["contactPerson"], "Suresh Shah — 91234 56789")

	def test_write_requires_purchase_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			supplier_api.create_supplier("SW Denied Co")
		with self.assertRaises(frappe.PermissionError):
			supplier_api.update_supplier("SW Update Co", {"disabled": True})

	def test_supplier_phone_email_and_gstin_set_and_clear(self):
		# Client finding 7.3 -- editable from the staff app, blank clears.
		created = supplier_api.create_supplier(
			"SW Contact Co", phone="9876500011", email="contact@swco.example", gstin="27aapfu0939f1zv"
		)
		self.assertEqual(created["phone"], "9876500011")
		self.assertEqual(created["email"], "contact@swco.example")
		if frappe.get_meta("Supplier").has_field("gstin"):
			self.assertEqual(created["gstin"], "27AAPFU0939F1ZV")

		cleared = supplier_api.update_supplier(created["id"], {"phone": "", "email": "", "gstin": ""})
		self.assertFalse(cleared["phone"])
		self.assertFalse(cleared["email"])
		self.assertFalse(cleared["gstin"])

	def test_supplier_bank_account_is_stored_as_an_erpnext_bank_account(self):
		# Client finding 7.3 -- the supplier's bank details live in ERPNext's own Bank
		# Account (party_type Supplier), so Payment Entries pick up the same record.
		created = supplier_api.create_supplier(
			"SW Bank Co", bank={"bankName": "SW Test Bank", "accountNumber": "001122334455", "ifsc": "hdfc0001234"}
		)
		self.assertEqual(
			created["bank"],
			{"bankName": "SW Test Bank", "accountNumber": "001122334455", "ifsc": "HDFC0001234", "accountName": "SW Bank Co"},
		)
		self.assertTrue(frappe.db.exists("Bank Account", {"party_type": "Supplier", "party": created["id"], "is_default": 1}))

		updated = supplier_api.update_supplier(created["id"], {"bank": {"bankName": "SW Test Bank", "accountNumber": "999", "ifsc": ""}})
		self.assertEqual(updated["bank"]["accountNumber"], "999")
		self.assertFalse(updated["bank"]["ifsc"])
		self.assertEqual(frappe.db.count("Bank Account", {"party_type": "Supplier", "party": created["id"]}), 1)

		with self.assertRaises(frappe.ValidationError):
			supplier_api.update_supplier(created["id"], {"bank": {"bankName": "SW Test Bank", "accountNumber": "1", "ifsc": "BAD"}})
		with self.assertRaises(frappe.ValidationError):
			supplier_api.update_supplier(created["id"], {"bank": {"bankName": "", "accountNumber": "1"}})

		removed = supplier_api.update_supplier(created["id"], {"bank": None})
		self.assertIsNone(removed["bank"])
		self.assertFalse(frappe.db.exists("Bank Account", {"party_type": "Supplier", "party": created["id"]}))
