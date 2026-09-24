from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.comms import whats91

TEST_AUTH_TOKEN = "unit-test-w91-token"
TEST_TEMPLATE = "dealer_otp_auth"


class _FakeResponse:
	def __init__(self, status_code=200, json_body=None, text=""):
		self.status_code = status_code
		self._json_body = json_body
		self.text = text or (str(json_body) if json_body is not None else "")

	def json(self):
		if self._json_body is None:
			raise ValueError("no json")
		return self._json_body


class TestWhats91(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.conf.dms_erp_whats91_auth_token = TEST_AUTH_TOKEN
		frappe.conf.dms_erp_whats91_otp_template = TEST_TEMPLATE

	@classmethod
	def tearDownClass(cls):
		frappe.conf.pop("dms_erp_whats91_auth_token", None)
		frappe.conf.pop("dms_erp_whats91_otp_template", None)
		super().tearDownClass()

	def test_unconfigured_site_returns_false_without_raising(self):
		frappe.conf.pop("dms_erp_whats91_auth_token", None)
		try:
			self.assertFalse(whats91.send_otp_template("9900011122", "123456"))
		finally:
			frappe.conf.dms_erp_whats91_auth_token = TEST_AUTH_TOKEN

	def test_invalid_phone_returns_false_without_calling_whats91(self):
		with patch("dms_erp.comms.whats91.requests.post") as mock_post:
			self.assertFalse(whats91.send_otp_template("12345", "123456"))
			mock_post.assert_not_called()

	def test_sends_the_expected_authentication_template_payload(self):
		with patch("dms_erp.comms.whats91.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(200, {"success": True})
			result = whats91.send_otp_template("9900011122", "654321")

		self.assertTrue(result)
		mock_post.assert_called_once()
		_args, kwargs = mock_post.call_args
		self.assertEqual(kwargs["json"], {
			"authToken": TEST_AUTH_TOKEN,
			"receiverId": "919900011122",
			"templateName": TEST_TEMPLATE,
			"parameters": ["654321"],
			"buttonParameters": ["654321"],
		})

	def test_strips_a_leading_91_country_code_before_re_adding_it(self):
		with patch("dms_erp.comms.whats91.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(200, {"success": True})
			whats91.send_otp_template("919900011122", "111111")

		_args, kwargs = mock_post.call_args
		self.assertEqual(kwargs["json"]["receiverId"], "919900011122")

	def test_whats91_reported_failure_returns_false(self):
		with patch("dms_erp.comms.whats91.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(200, {"success": False, "error": "invalid template"})
			self.assertFalse(whats91.send_otp_template("9900011122", "123456"))

	def test_non_200_http_status_returns_false(self):
		with patch("dms_erp.comms.whats91.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(500, text="Internal Server Error")
			self.assertFalse(whats91.send_otp_template("9900011122", "123456"))

	def test_network_error_returns_false_without_raising(self):
		import requests

		with patch("dms_erp.comms.whats91.requests.post", side_effect=requests.ConnectionError("boom")):
			self.assertFalse(whats91.send_otp_template("9900011122", "123456"))
