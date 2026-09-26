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


TEST_WEBHOOK_TOKEN = "unit-test-w91-webhook-token"
TEST_WEBHOOK_SECRET = "unit-test-webhook-secret"


class TestWhats91WebhookTokenVerification(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.conf.dms_erp_whats91_webhook_token = TEST_WEBHOOK_TOKEN

	@classmethod
	def tearDownClass(cls):
		frappe.conf.pop("dms_erp_whats91_webhook_token", None)
		super().tearDownClass()

	def test_accepts_the_matching_token(self):
		whats91._verify_whats91_webhook_token(TEST_WEBHOOK_TOKEN)  # must not raise

	def test_rejects_a_wrong_token(self):
		with self.assertRaises(frappe.PermissionError):
			whats91._verify_whats91_webhook_token("wrong-token")

	def test_rejects_a_missing_token(self):
		with self.assertRaises(frappe.PermissionError):
			whats91._verify_whats91_webhook_token(None)

	def test_throws_a_clear_error_when_not_configured_at_all(self):
		frappe.conf.pop("dms_erp_whats91_webhook_token", None)
		try:
			with self.assertRaises(frappe.ValidationError):
				whats91._verify_whats91_webhook_token(TEST_WEBHOOK_TOKEN)
		finally:
			frappe.conf.dms_erp_whats91_webhook_token = TEST_WEBHOOK_TOKEN


class _FakeHeaders:
	def __init__(self, headers: dict):
		self._headers = headers

	def get(self, key, default=None):
		return self._headers.get(key, default)


class _FakeRequest:
	def __init__(self, headers: dict):
		self.headers = _FakeHeaders(headers)


class TestWhats91ReceiveWebhook(FrappeTestCase):
	"""resolve_item_mention/webhook_inbound_message's own orchestration (dealer
	resolution, item matching, auto-reply) is tested where those live
	(comms.test_api, catalog.test_products) -- these only verify this adapter's own
	job: token check, event-shape translation, and safely no-op'ing on anything it
	doesn't yet handle."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.conf.dms_erp_whats91_webhook_token = TEST_WEBHOOK_TOKEN
		frappe.conf.dms_erp_whatsapp_webhook_secret = TEST_WEBHOOK_SECRET

	@classmethod
	def tearDownClass(cls):
		frappe.conf.pop("dms_erp_whats91_webhook_token", None)
		frappe.conf.pop("dms_erp_whatsapp_webhook_secret", None)
		super().tearDownClass()

	def setUp(self):
		self._orig_request = frappe.local.request
		frappe.local.request = _FakeRequest({whats91.WHATS91_WEBHOOK_HEADER_DEFAULT: TEST_WEBHOOK_TOKEN})
		frappe.local.response.pop("success", None)

	def tearDown(self):
		frappe.local.request = self._orig_request
		frappe.local.response.pop("success", None)

	def test_rejects_a_call_with_the_wrong_header_token(self):
		frappe.local.request = _FakeRequest({whats91.WHATS91_WEBHOOK_HEADER_DEFAULT: "wrong"})
		with self.assertRaises(frappe.PermissionError):
			whats91.receive_webhook(event="message.inbound.text", data={"from": "919900011122", "text": "hi"})

	@patch("dms_erp.comms.api.webhook_inbound_message")
	def test_translates_an_inbound_text_event_into_the_generic_webhook_call(self, mock_webhook):
		result = whats91.receive_webhook(
			event="message.inbound.text",
			data={
				"from": "919900011122",
				"messageId": "wamid.HBgMOTE5...",
				"text": "GVT 6013 stock hai kya",
				"timestamp": "2026-06-05T10:30:00.000Z",
			},
		)

		mock_webhook.assert_called_once_with(
			secret=TEST_WEBHOOK_SECRET,
			phone="919900011122",
			text="GVT 6013 stock hai kya",
			sent_at="2026-06-05T10:30:00.000Z",
		)
		self.assertEqual(result, {"success": True})
		self.assertTrue(frappe.local.response.get("success"))

	def test_rejects_a_malformed_inbound_text_event(self):
		with self.assertRaises(frappe.ValidationError):
			whats91.receive_webhook(event="message.inbound.text", data={"text": "hi, no sender"})
		with self.assertRaises(frappe.ValidationError):
			whats91.receive_webhook(event="message.inbound.text", data={"from": "919900011122"})

	@patch("dms_erp.comms.api.webhook_inbound_message")
	def test_acknowledges_but_does_not_act_on_an_unhandled_event(self, mock_webhook):
		result = whats91.receive_webhook(
			event="message.status.delivered",
			data={"messageId": "wamid.HBgMOTE5...", "recipient": "919900011122", "status": "delivered"},
		)

		mock_webhook.assert_not_called()
		self.assertEqual(result, {"success": True})
		self.assertTrue(frappe.local.response.get("success"))

	@patch("dms_erp.comms.api.webhook_inbound_message")
	def test_tolerates_unexpected_extra_top_level_fields(self, mock_webhook):
		# whats91's own examples show webhookUid/senderId alongside event/data --
		# **kwargs on receive_webhook must swallow those without a TypeError.
		result = whats91.receive_webhook(
			event="message.inbound.text",
			data={"from": "919900011122", "text": "hi"},
			webhookUid="wh_abc",
			senderId="916268662275",
		)
		self.assertEqual(result, {"success": True})
		self.assertTrue(frappe.local.response.get("success"))
