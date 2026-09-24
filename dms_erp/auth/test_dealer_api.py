import re

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.auth import dealer_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer

TEST_PHONE = "9900011122"


class TestDealerAuthApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("OTP Test Dealer")
		# custom_phone is stored normalized (see phone_utils.clean_indian_mobile /
		# sales.dealer_api._clean_phone_or_throw) -- bare 10 digits, exactly as
		# create_dealer/update_dealer would actually store it.
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", TEST_PHONE)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Dealer Login OTP", {"dealer": self.dealer})
		frappe.db.delete("WhatsApp Message", {"dealer": self.dealer})
		user = frappe.db.get_value("User", {"custom_dealer": self.dealer}, "name")
		if user:
			frappe.delete_doc("User", user, force=True, ignore_permissions=True)

	def _latest_otp_code(self) -> str:
		# The plaintext code is never persisted (only its hash) -- it's only ever
		# observable in the WhatsApp message it was delivered in, same as a real dealer
		# would read it off their phone.
		text = frappe.db.get_value("WhatsApp Message", {"dealer": self.dealer}, "text", order_by="creation desc")
		return re.search(r"\b(\d{6})\b", text).group(1)

	def test_request_otp_creates_otp_and_sends_whatsapp_message(self):
		result = dealer_api.request_otp(phone=TEST_PHONE)
		self.assertEqual(result, {"success": True})
		self.assertTrue(frappe.db.exists("Dealer Login OTP", {"dealer": self.dealer}))
		self.assertTrue(frappe.db.exists("WhatsApp Message", {"dealer": self.dealer, "direction": "Outbound"}))

	def test_request_otp_for_unregistered_phone_returns_same_generic_response_and_sends_nothing(self):
		result = dealer_api.request_otp(phone="9999999999")
		self.assertEqual(result, {"success": True})
		self.assertEqual(frappe.db.count("Dealer Login OTP"), 0)
		self.assertEqual(frappe.db.count("WhatsApp Message"), 0)

	def test_request_otp_for_malformed_phone_returns_same_generic_response_and_sends_nothing(self):
		result = dealer_api.request_otp(phone="not-a-phone-number")
		self.assertEqual(result, {"success": True})
		self.assertEqual(frappe.db.count("Dealer Login OTP"), 0)

	def test_request_otp_matches_the_dealer_regardless_of_how_the_phone_is_formatted(self):
		# The exact bug this normalization fixes: a phone typed with a +91 prefix
		# and spaces must still resolve to the same dealer whose custom_phone is
		# stored as bare digits.
		for variant in ("+91 99000 11122", "0" + TEST_PHONE, "91" + TEST_PHONE, TEST_PHONE):
			frappe.db.delete("Dealer Login OTP", {"dealer": self.dealer})
			frappe.db.delete("WhatsApp Message", {"dealer": self.dealer})
			dealer_api.request_otp(phone=variant)
			self.assertTrue(
				frappe.db.exists("Dealer Login OTP", {"dealer": self.dealer}),
				f"phone variant {variant!r} did not resolve to the dealer",
			)

	def test_request_otp_respects_the_resend_cooldown(self):
		dealer_api.request_otp(phone=TEST_PHONE)
		dealer_api.request_otp(phone=TEST_PHONE)
		self.assertEqual(frappe.db.count("Dealer Login OTP", {"dealer": self.dealer}), 1)
		self.assertEqual(frappe.db.count("WhatsApp Message", {"dealer": self.dealer}), 1)

	def test_verify_otp_success_creates_portal_user_and_issues_tokens(self):
		dealer_api.request_otp(phone=TEST_PHONE)
		code = self._latest_otp_code()

		tokens = dealer_api.verify_otp(phone=TEST_PHONE, otp=code, device_id="dev-1")
		self.assertTrue(tokens["access_token"])
		self.assertTrue(tokens["refresh_token"])
		self.assertEqual(tokens["user"]["dealer"], self.dealer)
		self.assertIn("DMS Dealer", tokens["user"]["roles"])

		user = frappe.db.get_value("User", {"custom_dealer": self.dealer}, "name")
		self.assertIsNotNone(user)
		self.assertIsNotNone(frappe.db.get_value("Dealer Login OTP", {"dealer": self.dealer}, "consumed_at"))

	def test_verify_otp_reuses_the_same_portal_user_on_a_second_login(self):
		dealer_api.request_otp(phone=TEST_PHONE)
		first_tokens = dealer_api.verify_otp(phone=TEST_PHONE, otp=self._latest_otp_code(), device_id="dev-1")

		dealer_api.request_otp(phone=TEST_PHONE)
		second_tokens = dealer_api.verify_otp(phone=TEST_PHONE, otp=self._latest_otp_code(), device_id="dev-2")

		self.assertEqual(first_tokens["user"]["name"], second_tokens["user"]["name"])
		self.assertEqual(frappe.db.count("User", {"custom_dealer": self.dealer}), 1)

	def test_verify_otp_wrong_code_raises_generic_error_and_counts_the_attempt(self):
		dealer_api.request_otp(phone=TEST_PHONE)

		with self.assertRaises(frappe.AuthenticationError):
			dealer_api.verify_otp(phone=TEST_PHONE, otp="000000", device_id="dev-1")

		otp_name = frappe.db.get_value("Dealer Login OTP", {"dealer": self.dealer}, "name")
		self.assertEqual(frappe.db.get_value("Dealer Login OTP", otp_name, "attempts"), 1)

	def test_verify_otp_locks_out_after_max_attempts(self):
		dealer_api.request_otp(phone=TEST_PHONE)

		for _ in range(dealer_api.MAX_OTP_ATTEMPTS):
			with self.assertRaises(frappe.AuthenticationError):
				dealer_api.verify_otp(phone=TEST_PHONE, otp="000000", device_id="dev-1")

		code = self._latest_otp_code()
		with self.assertRaises(frappe.AuthenticationError):
			dealer_api.verify_otp(phone=TEST_PHONE, otp=code, device_id="dev-1")

	def test_verify_otp_for_unknown_phone_raises_the_same_generic_error(self):
		with self.assertRaises(frappe.AuthenticationError):
			dealer_api.verify_otp(phone="+910000000000", otp="123456", device_id="dev-1")

	def test_verify_otp_expired_code_raises(self):
		dealer_api.request_otp(phone=TEST_PHONE)
		otp_name = frappe.db.get_value("Dealer Login OTP", {"dealer": self.dealer}, "name")
		frappe.db.set_value("Dealer Login OTP", otp_name, "expires_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1))

		with self.assertRaises(frappe.AuthenticationError):
			dealer_api.verify_otp(phone=TEST_PHONE, otp=self._latest_otp_code(), device_id="dev-1")

	def test_verify_otp_cannot_reuse_an_already_consumed_code(self):
		dealer_api.request_otp(phone=TEST_PHONE)
		code = self._latest_otp_code()
		dealer_api.verify_otp(phone=TEST_PHONE, otp=code, device_id="dev-1")

		with self.assertRaises(frappe.AuthenticationError):
			dealer_api.verify_otp(phone=TEST_PHONE, otp=code, device_id="dev-2")
