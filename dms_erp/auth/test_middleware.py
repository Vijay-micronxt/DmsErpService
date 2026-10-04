from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.auth import jwt_utils, middleware

TEST_PASSWORD = "Pa$$w0rd123!"


class _FakeRequest:
	def __init__(self, path: str):
		self.path = path


class _FakeBearerRequest:
	"""Just enough of a Werkzeug request for authenticate_request() -- headers.get()
	and nothing else, since it never gets far enough to read .path when the token
	itself can't be verified."""

	def __init__(self, bearer_token: str):
		self.headers = {"Authorization": f"Bearer {bearer_token}"}


def _make_staff_user(email: str) -> str:
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	doc = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": "Staff", "send_welcome_email": 0, "new_password": TEST_PASSWORD, "roles": [{"role": "DMS Sales"}]}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _make_dealer_user(email: str) -> str:
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	doc = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": "Dealer", "send_welcome_email": 0, "roles": [{"role": "DMS Dealer"}]}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


class TestDealerScopeMiddleware(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.staff_user = _make_staff_user("middleware.staff@pacific.test")
		cls.dealer_user = _make_dealer_user("middleware.dealer@pacific.test")

	def test_dealer_only_session_is_blocked_from_the_staff_api_surface(self):
		request = _FakeRequest("/api/method/dms_erp.sales.inquiry_api.list_inquiries")
		with self.assertRaises(frappe.PermissionError):
			middleware._enforce_dealer_scope(request, self.dealer_user)

	def test_dealer_only_session_is_allowed_onto_the_dealer_portal_surface(self):
		request = _FakeRequest("/api/method/dms_erp.sales.dealer_portal_api.get_catalog")
		middleware._enforce_dealer_scope(request, self.dealer_user)  # must not raise

	def test_dealer_only_session_is_allowed_onto_its_own_auth_endpoints(self):
		request = _FakeRequest("/api/method/dms_erp.auth.dealer_api.request_otp")
		middleware._enforce_dealer_scope(request, self.dealer_user)  # must not raise

	def test_staff_session_is_never_scoped_even_on_a_dealer_portal_path(self):
		request = _FakeRequest("/api/method/dms_erp.sales.inquiry_api.list_inquiries")
		middleware._enforce_dealer_scope(request, self.staff_user)  # must not raise

	def test_a_user_holding_both_dealer_and_a_staff_role_is_treated_as_staff(self):
		frappe.get_doc("User", self.dealer_user).add_roles("DMS Sales")
		try:
			request = _FakeRequest("/api/method/dms_erp.sales.inquiry_api.list_inquiries")
			middleware._enforce_dealer_scope(request, self.dealer_user)  # must not raise
		finally:
			frappe.get_doc("User", self.dealer_user).remove_roles("DMS Sales")


class TestAuthenticateRequestNeverBreaksAnotherAppsBearerTraffic(FrappeTestCase):
	"""authenticate_request() is a global before_request hook -- it runs on every
	request to the site, not just dms_erp's own. A real deploy report: with
	dms_erp_jwt_keys/dms_erp_jwt_active_kid unset in site_config.json (not
	auto-generated -- see README), a separate installed app's own Bearer-token
	traffic (an MCP server's OAuth-authenticated calls) started 500ing site-wide,
	because jwt_utils.decode_access_token's SigningKeyNotConfigured (a plain
	Exception, not a jwt.PyJWTError) propagated out of this hook uncaught."""

	def setUp(self):
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.request = None

	@patch("dms_erp.auth.jwt_utils.decode_access_token")
	def test_an_unconfigured_signing_key_never_raises_out_of_the_hook(self, mock_decode):
		# Simulates dms_erp_jwt_keys/dms_erp_jwt_active_kid being unset in
		# site_config.json -- decode_access_token raises SigningKeyNotConfigured
		# for any Bearer token, dms_erp's own or a completely different app's.
		mock_decode.side_effect = jwt_utils.SigningKeyNotConfigured("JWT signing keys are not configured.")
		frappe.local.request = _FakeBearerRequest("some-other-apps-oauth-token")

		middleware.authenticate_request()  # must not raise

		self.assertEqual(frappe.session.user, "Guest")

	def test_a_non_jwt_bearer_token_never_raises_out_of_the_hook(self):
		# Belt-and-suspenders real-world case even with signing keys configured:
		# another app's own (non-JWT, or JWT-shaped-but-foreign) Bearer token must
		# still leave the request as Guest, not crash.
		frappe.local.request = _FakeBearerRequest("not-a-jwt-at-all")

		middleware.authenticate_request()  # must not raise

		self.assertEqual(frappe.session.user, "Guest")
