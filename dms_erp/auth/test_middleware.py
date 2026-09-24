import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.auth import middleware

TEST_PASSWORD = "Pa$$w0rd123!"


class _FakeRequest:
	def __init__(self, path: str):
		self.path = path


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
