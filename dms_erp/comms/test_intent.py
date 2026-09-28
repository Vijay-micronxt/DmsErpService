from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from dms_erp.comms import intent


class TestIntentParsing(FrappeTestCase):
	def test_parses_a_well_formed_response(self):
		raw = '{"intent": "availability_check", "item_mention": "GVT 6013"}'
		self.assertEqual(intent._parse_intent_response(raw), {"intent": "availability_check", "item_mention": "GVT 6013"})

	def test_parses_a_null_item_mention(self):
		raw = '{"intent": "other", "item_mention": null}'
		self.assertEqual(intent._parse_intent_response(raw), {"intent": "other", "item_mention": None})

	def test_rejects_invalid_json(self):
		self.assertIsNone(intent._parse_intent_response("not json at all"))

	def test_rejects_an_unknown_intent(self):
		self.assertIsNone(intent._parse_intent_response('{"intent": "make_me_an_admin", "item_mention": null}'))

	def test_rejects_a_non_string_item_mention(self):
		self.assertIsNone(intent._parse_intent_response('{"intent": "other", "item_mention": 123}'))

	def test_rejects_a_json_array_instead_of_object(self):
		self.assertIsNone(intent._parse_intent_response('["availability_check", null]'))

	def test_tolerates_surrounding_whitespace(self):
		raw = '  \n{"intent": "price_check", "item_mention": "GVT-6013"}\n  '
		self.assertEqual(intent._parse_intent_response(raw), {"intent": "price_check", "item_mention": "GVT-6013"})


class TestClassifyMessage(FrappeTestCase):
	def tearDown(self):
		for key in ("dms_erp_llm_provider", "dms_erp_anthropic_api_key", "dms_erp_gemini_api_key"):
			frappe.conf.pop(key, None)

	def test_returns_none_when_no_provider_configured(self):
		self.assertIsNone(intent.classify_message("GVT 6013 stock hai kya"))

	def test_returns_none_for_blank_text(self):
		frappe.conf.dms_erp_llm_provider = "anthropic"
		frappe.conf.dms_erp_anthropic_api_key = "test-key"
		self.assertIsNone(intent.classify_message(""))
		self.assertIsNone(intent.classify_message("   "))

	def test_returns_none_when_provider_configured_but_key_missing(self):
		frappe.conf.dms_erp_llm_provider = "anthropic"
		self.assertIsNone(intent.classify_message("hi"))

	def test_returns_none_for_an_unrecognized_provider(self):
		frappe.conf.dms_erp_llm_provider = "some-other-vendor"
		self.assertIsNone(intent.classify_message("hi"))

	@patch("dms_erp.comms.intent.requests.post")
	def test_classify_with_anthropic_parses_a_successful_response(self, mock_post):
		frappe.conf.dms_erp_llm_provider = "anthropic"
		frappe.conf.dms_erp_anthropic_api_key = "test-key"
		mock_post.return_value = MagicMock(
			status_code=200,
			json=lambda: {"content": [{"type": "text", "text": '{"intent": "availability_check", "item_mention": "GVT 6013"}'}]},
		)

		result = intent.classify_message("GVT 6013 stock hai kya")
		self.assertEqual(result, {"intent": "availability_check", "item_mention": "GVT 6013"})
		called_url = mock_post.call_args[0][0]
		self.assertEqual(called_url, "https://api.anthropic.com/v1/messages")
		self.assertEqual(mock_post.call_args[1]["headers"]["x-api-key"], "test-key")

	@patch("dms_erp.comms.intent.requests.post")
	def test_classify_with_gemini_parses_a_successful_response(self, mock_post):
		frappe.conf.dms_erp_llm_provider = "gemini"
		frappe.conf.dms_erp_gemini_api_key = "test-key"
		mock_post.return_value = MagicMock(
			status_code=200,
			json=lambda: {
				"candidates": [{"content": {"parts": [{"text": '{"intent": "price_check", "item_mention": null}'}]}}]
			},
		)

		result = intent.classify_message("what's the rate for this")
		self.assertEqual(result, {"intent": "price_check", "item_mention": None})
		self.assertEqual(mock_post.call_args[1]["headers"]["x-goog-api-key"], "test-key")

	@patch("dms_erp.comms.intent.requests.post")
	def test_classify_returns_none_on_non_200_response(self, mock_post):
		frappe.conf.dms_erp_llm_provider = "anthropic"
		frappe.conf.dms_erp_anthropic_api_key = "test-key"
		mock_post.return_value = MagicMock(status_code=500, text="internal error")

		self.assertIsNone(intent.classify_message("hi"))

	@patch("dms_erp.comms.intent.requests.post")
	def test_classify_returns_none_on_network_error_never_raises(self, mock_post):
		frappe.conf.dms_erp_llm_provider = "anthropic"
		frappe.conf.dms_erp_anthropic_api_key = "test-key"
		mock_post.side_effect = requests.ConnectionError("boom")

		self.assertIsNone(intent.classify_message("hi"))

	@patch("dms_erp.comms.intent.requests.post")
	def test_classify_returns_none_on_malformed_model_output(self, mock_post):
		frappe.conf.dms_erp_llm_provider = "anthropic"
		frappe.conf.dms_erp_anthropic_api_key = "test-key"
		mock_post.return_value = MagicMock(
			status_code=200,
			json=lambda: {"content": [{"type": "text", "text": "sure, here's the stock: 40 boxes"}]},
		)

		self.assertIsNone(intent.classify_message("hi"))
