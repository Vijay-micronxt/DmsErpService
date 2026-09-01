import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.auth import api

TEST_JWT_KEYS = {"test-kid": "unit-test-signing-secret"}
TEST_PASSWORD = "Pa$$w0rd123!"


def make_staff_user(email, role="DMS Sales"):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True)
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"new_password": TEST_PASSWORD,
			"roles": [{"role": role}],
		}
	)
	user.insert(ignore_permissions=True)
	return user.name


class TestAuthSession(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.conf.dms_erp_jwt_keys = TEST_JWT_KEYS
		frappe.conf.dms_erp_jwt_active_kid = "test-kid"
		cls.staff_user = make_staff_user("staff.tester@pacific.test")
		cls.plain_user = make_staff_user("plain.tester@pacific.test", role="Sales User")

	@classmethod
	def tearDownClass(cls):
		frappe.conf.pop("dms_erp_jwt_keys", None)
		frappe.conf.pop("dms_erp_jwt_active_kid", None)
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_login_success_issues_tokens_and_session(self):
		result = api.login(usr=self.staff_user, pwd=TEST_PASSWORD, device_id="dev-1", device_name="Test Phone")
		self.assertTrue(result["access_token"])
		self.assertTrue(result["refresh_token"])
		self.assertEqual(result["user"]["primary_role"], "sales")
		self.assertTrue(frappe.db.exists("Auth Session", {"user": self.staff_user, "device_id": "dev-1"}))

	def test_login_wrong_password_raises(self):
		with self.assertRaises(frappe.AuthenticationError):
			api.login(usr=self.staff_user, pwd="wrong-password", device_id="dev-1")

	def test_login_rejects_non_staff_role(self):
		with self.assertRaises(frappe.PermissionError):
			api.login(usr=self.plain_user, pwd=TEST_PASSWORD, device_id="dev-1")

	def test_refresh_rotates_and_old_token_is_rejected(self):
		tokens = api.login(usr=self.staff_user, pwd=TEST_PASSWORD, device_id="dev-2")
		old_refresh = tokens["refresh_token"]

		rotated = api.refresh_token(refresh_token=old_refresh)
		self.assertTrue(rotated["access_token"])
		self.assertNotEqual(rotated["refresh_token"], old_refresh)

		with self.assertRaises(frappe.AuthenticationError):
			api.refresh_token(refresh_token=old_refresh)

	def test_refresh_reuse_revokes_session(self):
		tokens = api.login(usr=self.staff_user, pwd=TEST_PASSWORD, device_id="dev-3")
		old_refresh = tokens["refresh_token"]
		rotated = api.refresh_token(refresh_token=old_refresh)

		with self.assertRaises(frappe.AuthenticationError):
			api.refresh_token(refresh_token=old_refresh)

		# The session is now revoked outright, so even the *rotated* (newest) token
		# must also stop working.
		with self.assertRaises(frappe.AuthenticationError):
			api.refresh_token(refresh_token=rotated["refresh_token"])

	def test_concurrent_refresh_race_raises_clean_auth_error_without_revoking(self):
		# Simulates two refresh_token calls racing on the same session (e.g. a
		# client that fires more than one 401-triggered refresh instead of
		# sharing one in-flight request): forcing Document.save to raise the
		# same TimestampMismatchError Frappe raises when the DB row changed
		# out from under a loaded doc, deterministically, without needing real
		# threading. Must come back as a clean AuthenticationError, not the
		# raw framework exception, and must NOT revoke the session -- this is
		# an ordinary race between two legitimate calls, not a replay.
		from unittest.mock import patch

		tokens = api.login(usr=self.staff_user, pwd=TEST_PASSWORD, device_id="dev-race")

		with patch("frappe.model.document.Document.save", side_effect=frappe.TimestampMismatchError):
			with self.assertRaises(frappe.AuthenticationError):
				api.refresh_token(refresh_token=tokens["refresh_token"])

		session_name = frappe.db.get_value("Auth Session", {"user": self.staff_user, "device_id": "dev-race"}, "name")
		self.assertIsNone(frappe.db.get_value("Auth Session", session_name, "revoked_at"))

	def test_logout_revokes_session(self):
		tokens = api.login(usr=self.staff_user, pwd=TEST_PASSWORD, device_id="dev-4")
		api.logout(refresh_token=tokens["refresh_token"])

		with self.assertRaises(frappe.AuthenticationError):
			api.refresh_token(refresh_token=tokens["refresh_token"])

	def test_logout_all_revokes_every_session(self):
		tokens_a = api.login(usr=self.staff_user, pwd=TEST_PASSWORD, device_id="dev-a")
		tokens_b = api.login(usr=self.staff_user, pwd=TEST_PASSWORD, device_id="dev-b")

		frappe.set_user(self.staff_user)
		api.logout_all()
		frappe.set_user("Administrator")

		with self.assertRaises(frappe.AuthenticationError):
			api.refresh_token(refresh_token=tokens_a["refresh_token"])
		with self.assertRaises(frappe.AuthenticationError):
			api.refresh_token(refresh_token=tokens_b["refresh_token"])

	def test_me_returns_profile_for_authenticated_user(self):
		frappe.set_user(self.staff_user)
		profile = api.me()
		self.assertEqual(profile["name"], self.staff_user)
		self.assertIn("DMS Sales", profile["roles"])
