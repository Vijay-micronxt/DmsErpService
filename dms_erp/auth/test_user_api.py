import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.auth import user_api

PASSWORD = "Tr!cky-Passw0rd-2026"


class TestUserApi(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def _make(self, email, roles=("DMS Warehouse",)):
		return user_api.create_user(email, "Test " + email.split("@")[0], PASSWORD, list(roles))

	def test_create_user_grants_only_the_requested_dms_roles(self):
		user = self._make("ua.create@pacific.example", ("DMS Sales", "DMS Warehouse"))
		self.assertTrue(user["enabled"])
		self.assertEqual(set(user["appRoles"]), {"sales", "warehouse"})

	def test_create_user_refuses_non_dms_roles_duplicates_and_empty_roles(self):
		with self.assertRaises(frappe.ValidationError):
			user_api.create_user("ua.sm@pacific.example", "SM", PASSWORD, ["System Manager"])
		with self.assertRaises(frappe.ValidationError):
			user_api.create_user("ua.none@pacific.example", "None", PASSWORD, [])
		self._make("ua.dup@pacific.example")
		with self.assertRaises(frappe.DuplicateEntryError):
			self._make("ua.dup@pacific.example")

	def test_update_user_replaces_dms_roles_and_disable_revokes_sessions(self):
		self._make("ua.update@pacific.example")
		updated = user_api.update_user("ua.update@pacific.example", {"fullName": "Renamed", "roles": ["DMS Purchase"]})
		self.assertEqual(updated["appRoles"], ["purchase"])
		self.assertEqual(updated["fullName"], "Renamed")

		frappe.get_doc(
			{
				"doctype": "Auth Session",
				"user": "ua.update@pacific.example",
				"device_id": "ua-test",
				"refresh_token_hash": "ua-test-hash",
				"issued_at": frappe.utils.now_datetime(),
				"expires_at": frappe.utils.add_days(frappe.utils.now_datetime(), 1),
			}
		).insert(ignore_permissions=True)
		self.assertFalse(user_api.update_user("ua.update@pacific.example", {"enabled": False})["enabled"])
		self.assertTrue(frappe.db.get_value("Auth Session", {"refresh_token_hash": "ua-test-hash"}, "revoked_at"))

	def test_update_user_refuses_an_empty_role_set(self):
		self._make("ua.empty@pacific.example")
		with self.assertRaises(frappe.ValidationError):
			user_api.update_user("ua.empty@pacific.example", {"roles": []})

	def test_list_users_filters_by_role_and_hides_system_accounts(self):
		self._make("ua.list@pacific.example", ("DMS Warehouse",))
		ids = [u["id"] for u in user_api.list_users(role="DMS Warehouse")["items"]]
		self.assertIn("ua.list@pacific.example", ids)
		everyone = [u["id"] for u in user_api.list_users(limit=100)["items"]]
		self.assertNotIn("Administrator", everyone)
		self.assertNotIn("Guest", everyone)
		with self.assertRaises(frappe.ValidationError):
			user_api.list_users(role="System Manager")

	def test_only_management_can_manage_users_and_administrator_is_protected(self):
		self._make("ua.actor@pacific.example", ("DMS Warehouse",))
		frappe.set_user("ua.actor@pacific.example")
		with self.assertRaises(frappe.PermissionError):
			user_api.create_user("ua.x@pacific.example", "X", PASSWORD, ["DMS Sales"])
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			user_api.update_user("Administrator", {"fullName": "nope"})
