import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import update_password

from dms_erp.auth.api import _issue_tokens
from dms_erp.auth.dealer_api import _dealer_portal_user
from dms_erp.sales import dealer_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer
from dms_erp.warehouse.utils import default_company


class TestDealerApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Dealer Api Test Co")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_get_dealer_returns_native_customer_fields(self):
		result = dealer_api.get_dealer(self.dealer)
		self.assertEqual(result["id"], self.dealer)
		self.assertEqual(result["name"], self.dealer)
		self.assertEqual(result["group"], "All Customer Groups")
		self.assertEqual(result["territory"], "All Territories")
		self.assertFalse(result["disabled"])

	def test_credit_limit_reads_from_the_child_table_not_a_flat_column(self):
		# Customer.credit_limit is a Customer Credit Limit child row keyed by
		# company on modern ERPNext, not a flat column — see dealer_api.py's
		# module docstring for why.
		doc = frappe.get_doc("Customer", self.dealer)
		doc.append("credit_limits", {"company": default_company(), "credit_limit": 250000})
		doc.save(ignore_permissions=True)

		self.assertEqual(dealer_api.get_dealer(self.dealer)["creditLimit"], 250000)

		listed = next(r for r in dealer_api.list_dealers(search="Dealer Api Test") if r["id"] == self.dealer)
		self.assertEqual(listed["creditLimit"], 250000)

	def test_dealer_with_no_credit_limit_set_defaults_to_zero(self):
		bare_dealer = make_dealer("Dealer Api No Credit Limit Co")
		self.assertEqual(dealer_api.get_dealer(bare_dealer)["creditLimit"], 0)

	def test_list_dealers_filters_by_search_and_excludes_disabled_by_default(self):
		results = dealer_api.list_dealers(search="Dealer Api Test")
		self.assertIn(self.dealer, [r["id"] for r in results])

		frappe.db.set_value("Customer", self.dealer, "disabled", 1)
		try:
			active = dealer_api.list_dealers(search="Dealer Api Test")
			self.assertNotIn(self.dealer, [r["id"] for r in active])

			disabled_only = dealer_api.list_dealers(search="Dealer Api Test", disabled=True)
			self.assertIn(self.dealer, [r["id"] for r in disabled_only])
		finally:
			frappe.db.set_value("Customer", self.dealer, "disabled", 0)

	def test_set_dealer_salesperson_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			dealer_api.set_dealer_salesperson(self.dealer, "priya@pacific.example")

	def test_create_dealer_rejects_an_email_already_used_by_another_dealer(self):
		# Email now doubles as a login-password lookup key (auth.dealer_api.
		# login_with_password) -- two dealers sharing one would make that
		# resolve to whichever one frappe.db.get_value happens to pick.
		dealer_api.create_dealer(name="Dealer Api Email Owner Co", email="shared@pacific.example")

		with self.assertRaises(frappe.ValidationError):
			dealer_api.create_dealer(name="Dealer Api Email Squatter Co", email="shared@pacific.example")

	def test_update_dealer_rejects_an_email_already_used_by_another_dealer(self):
		dealer_api.create_dealer(name="Dealer Api Email Taken Co", email="taken@pacific.example")
		other = dealer_api.create_dealer(name="Dealer Api Email Wants It Co")["id"]

		with self.assertRaises(frappe.ValidationError):
			dealer_api.update_dealer(other, {"email": "taken@pacific.example"})

	def test_update_dealer_allows_keeping_its_own_unchanged_email(self):
		dealer = dealer_api.create_dealer(name="Dealer Api Keep Own Email Co", email="keepme@pacific.example")["id"]

		result = dealer_api.update_dealer(dealer, {"email": "keepme@pacific.example"})
		self.assertEqual(result["email"], "keepme@pacific.example")


class TestDealerPortalStatus(FrappeTestCase):
	"""BRD C.13 staff-side onboarding visibility -- whether a dealer has ever
	actually logged into the portal, set a password, or neither, since nothing
	about that shows on the Customer form itself."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()

	def tearDown(self):
		frappe.set_user("Administrator")

	def _cleanup_portal_user(self, dealer: str):
		user = frappe.db.get_value("User", {"custom_dealer": dealer}, "name")
		if user:
			frappe.db.delete("Auth Session", {"user": user})
			frappe.delete_doc("User", user, force=True, ignore_permissions=True)

	def test_a_dealer_who_never_logged_in_shows_no_portal_account(self):
		dealer = make_dealer("Portal Status Never Logged In Co")
		result = dealer_api.get_dealer(dealer)
		self.assertFalse(result["portalAccountCreated"])
		self.assertFalse(result["hasPassword"])
		self.assertIsNone(result["lastLoginAt"])

	def test_a_dealer_who_logged_in_via_otp_but_never_set_a_password(self):
		dealer = make_dealer("Portal Status OTP Only Co")
		_dealer_portal_user(dealer)
		try:
			result = dealer_api.get_dealer(dealer)
			self.assertTrue(result["portalAccountCreated"])
			self.assertFalse(result["hasPassword"])
		finally:
			self._cleanup_portal_user(dealer)

	def test_a_dealer_who_has_set_a_password(self):
		dealer = make_dealer("Portal Status Password Set Co")
		user = _dealer_portal_user(dealer)
		update_password(user, "SuperSecret123")
		try:
			result = dealer_api.get_dealer(dealer)
			self.assertTrue(result["portalAccountCreated"])
			self.assertTrue(result["hasPassword"])
		finally:
			self._cleanup_portal_user(dealer)

	def test_last_login_reflects_the_most_recent_session(self):
		dealer = make_dealer("Portal Status Last Login Co")
		user = _dealer_portal_user(dealer)
		try:
			_issue_tokens(user, "dev-1", None)
			result = dealer_api.get_dealer(dealer)
			self.assertIsNotNone(result["lastLoginAt"])
		finally:
			self._cleanup_portal_user(dealer)

	def test_list_dealers_reports_portal_status_per_row(self):
		onboarded = make_dealer("Portal Status List Onboarded Co")
		not_onboarded = make_dealer("Portal Status List Not Onboarded Co")
		_dealer_portal_user(onboarded)
		try:
			results = dealer_api.list_dealers(search="Portal Status List")
			by_id = {r["id"]: r for r in results}
			self.assertTrue(by_id[onboarded]["portalAccountCreated"])
			self.assertFalse(by_id[not_onboarded]["portalAccountCreated"])
		finally:
			self._cleanup_portal_user(onboarded)
