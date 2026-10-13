from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import create_item_groups


class TestCreateItemGroups(FrappeTestCase):
	def test_real_site_with_an_existing_root_is_idempotent(self):
		# Every other test in this app already calls setup_catalog() against a real
		# site with ERPNext's own "All Item Groups" root present -- this just makes
		# that normal path an explicit, named test rather than only ever incidental
		# coverage from setUpClass() elsewhere.
		create_item_groups()
		create_item_groups()
		self.assertTrue(frappe.db.exists("Item Group", "Vitrified"))

	@patch("dms_erp.catalog.setup.frappe")
	def test_self_heals_when_no_item_group_root_exists_at_all(self, mock_frappe):
		# Real production report: a site installed purely via CLI (install-app
		# without an intervening `bench migrate`, or before ERPNext's Setup Wizard
		# ever ran) can reach dms_erp's install with zero Item Group rows --
		# LinkValidationError: Could not find Parent Item Group: All Item Groups.
		# No root found, and "All Item Groups" itself doesn't exist either --
		# create_item_groups must create it rather than assume it's there.
		mock_frappe.db.get_value.return_value = None
		mock_frappe.db.exists.return_value = False
		mock_doc = MagicMock()
		mock_frappe.get_doc.return_value = mock_doc

		create_item_groups()

		root_insert_calls = [
			call for call in mock_frappe.get_doc.call_args_list if call.args[0].get("item_group_name") == "All Item Groups"
		]
		self.assertEqual(len(root_insert_calls), 1)
		self.assertEqual(root_insert_calls[0].args[0]["is_group"], 1)
		mock_doc.insert.assert_any_call(ignore_permissions=True)
