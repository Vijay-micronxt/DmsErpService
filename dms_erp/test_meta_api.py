import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp import meta_api


class TestMetaApi(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_returns_field_options_for_an_allowlisted_doctype(self):
		options = meta_api.get_column_options("Inquiry")
		fieldnames = {o["fieldname"] for o in options}
		self.assertIn("dealer", fieldnames)
		self.assertIn("qty", fieldnames)
		self.assertIn("status", fieldnames)

	def test_excludes_layout_and_hidden_fields(self):
		options = meta_api.get_column_options("Inquiry")
		fieldtypes = {o["fieldtype"] for o in options}
		self.assertNotIn("Section Break", fieldtypes)
		self.assertNotIn("Column Break", fieldtypes)

	def test_marks_default_visibility_from_in_list_view(self):
		options = meta_api.get_column_options("Inquiry")
		by_name = {o["fieldname"]: o for o in options}
		# status is in_list_view=1 on the Inquiry doctype.
		self.assertTrue(by_name["status"]["defaultVisible"])

	def test_returns_options_in_field_order(self):
		options = meta_api.get_column_options("Inquiry")
		orders = [o["order"] for o in options]
		self.assertEqual(orders, sorted(orders))

	def test_rejects_a_doctype_not_on_the_allowlist(self):
		with self.assertRaises(frappe.ValidationError):
			meta_api.get_column_options("User")

	def test_rejects_a_caller_with_no_read_permission_on_the_doctype(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			meta_api.get_column_options("Inquiry")
