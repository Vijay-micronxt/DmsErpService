from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.comms import api as comms_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer

TEST_WEBHOOK_SECRET = "unit-test-webhook-secret"


class TestCommsApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		frappe.conf.dms_erp_whatsapp_webhook_secret = TEST_WEBHOOK_SECRET
		cls.dealer = make_dealer("Comms Test Dealer")

	@classmethod
	def tearDownClass(cls):
		frappe.conf.pop("dms_erp_whatsapp_webhook_secret", None)
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_send_message_creates_outbound_sent(self):
		message = comms_api.send_message(dealer=self.dealer, text="Checking stock, will confirm shortly.")
		self.assertEqual(message["direction"], "Outbound")
		self.assertEqual(message["status"], "Sent")
		self.assertEqual(message["sentBy"], "Administrator")

	def test_send_message_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			comms_api.send_message(dealer=self.dealer, text="hi")

	def test_list_messages_returns_ascending_by_sent_at(self):
		comms_api.send_message(dealer=self.dealer, text="first")
		comms_api.send_message(dealer=self.dealer, text="second")

		thread = comms_api.list_messages(self.dealer)["items"]
		self.assertEqual([m["text"] for m in thread[-2:]], ["first", "second"])
		self.assertEqual(comms_api.last_message(self.dealer)["text"], "second")

	def test_list_messages_is_paginated(self):
		dealer = make_dealer("Comms Pagination Dealer")
		for text in ("one", "two", "three"):
			comms_api.send_message(dealer=dealer, text=text)

		page = comms_api.list_messages(dealer, limit=2, offset=0)
		self.assertEqual(page["total"], 3)
		self.assertEqual(len(page["items"]), 2)
		self.assertEqual(page["limit"], 2)
		self.assertEqual(page["offset"], 0)
		self.assertEqual([m["text"] for m in page["items"]], ["one", "two"])

		next_page = comms_api.list_messages(dealer, limit=2, offset=2)
		self.assertEqual(len(next_page["items"]), 1)
		self.assertEqual(next_page["items"][0]["text"], "three")

	def test_webhook_inbound_message_rejects_wrong_secret(self):
		with self.assertRaises(frappe.PermissionError):
			comms_api.webhook_inbound_message(secret="wrong", dealer=self.dealer, text="hi")

	def test_webhook_inbound_message_creates_delivered_inbound(self):
		message = comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="Need 320 boxes urgently.")
		self.assertEqual(message["direction"], "Inbound")
		self.assertEqual(message["status"], "Delivered")
		self.assertIsNone(message["sentBy"])

	def test_webhook_inbound_message_accepts_an_iso_8601_sent_at(self):
		# whats91 (and WhatsApp Business API generally) sends timestamps like this --
		# MariaDB's Datetime column rejects the literal string outright (a real bug
		# found via a live end-to-end test, not a hypothetical), so this is a
		# regression test for _parse_sent_at doing the conversion first.
		message = comms_api.webhook_inbound_message(
			secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="Hi", sent_at="2026-09-26T09:00:00.000Z"
		)
		self.assertEqual(message["direction"], "Inbound")

	def test_webhook_inbound_message_falls_back_to_now_for_an_unparseable_sent_at(self):
		message = comms_api.webhook_inbound_message(
			secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="hi", sent_at="not a real timestamp"
		)
		self.assertEqual(message["direction"], "Inbound")

	def test_webhook_inbound_message_resolves_dealer_from_phone(self):
		dealer = make_dealer("Comms Phone Dealer")
		frappe.db.set_value("Customer", dealer, "custom_phone", "9620204657")

		# Differently formatted from how it's stored -- same normalization OTP login uses.
		message = comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, phone="+91 96202 04657", text="Stock hai kya")
		self.assertEqual(message["dealerId"], dealer)
		self.assertEqual(message["direction"], "Inbound")

	def test_webhook_inbound_message_rejects_an_unresolvable_phone(self):
		with self.assertRaises(frappe.ValidationError):
			comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, phone="9999999999", text="hi")

	def test_webhook_inbound_message_requires_dealer_or_phone(self):
		with self.assertRaises(frappe.ValidationError):
			comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, text="hi")

	def test_mark_read_transitions_inbound_message(self):
		message = comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="Any update?")
		updated = comms_api.mark_read(message["id"])
		self.assertEqual(updated["status"], "Read")

	def test_webhook_status_update_progresses_outbound_message(self):
		message = comms_api.send_message(dealer=self.dealer, text="Your order is confirmed.")
		updated = comms_api.webhook_status_update(secret=TEST_WEBHOOK_SECRET, message=message["id"], status="Delivered")
		self.assertEqual(updated["status"], "Delivered")
		updated = comms_api.webhook_status_update(secret=TEST_WEBHOOK_SECRET, message=message["id"], status="Read")
		self.assertEqual(updated["status"], "Read")

	def test_webhook_status_update_rejects_inbound_message(self):
		message = comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="hi")
		with self.assertRaises(frappe.ValidationError):
			comms_api.webhook_status_update(secret=TEST_WEBHOOK_SECRET, message=message["id"], status="Read")

	def test_unreplied_inbound_count(self):
		dealer = make_dealer("Comms Test Dealer 2")
		self.assertEqual(comms_api.unreplied_inbound_count(dealer), 0)

		comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=dealer, text="Need stock update")
		self.assertEqual(comms_api.unreplied_inbound_count(dealer), 1)

		comms_api.send_message(dealer=dealer, text="Checking now")
		self.assertEqual(comms_api.unreplied_inbound_count(dealer), 0)

	def test_list_templates_returns_seeded_templates(self):
		templates = comms_api.list_templates()
		self.assertTrue(any(t["label"] == "Item available" for t in templates))


class TestCommsAutoReply(FrappeTestCase):
	"""_maybe_auto_reply's own dependencies (item-mention resolution, the LLM
	classification call itself, catalog visibility/sellability gating) each have
	their own tests elsewhere (catalog.test_products, comms.test_intent,
	sales.test_inquiry_api) -- these only verify this module's orchestration: given
	those pieces behave a certain way, does the webhook correctly resolve, gate, and
	reply (or correctly do nothing)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		frappe.conf.dms_erp_whatsapp_webhook_secret = TEST_WEBHOOK_SECRET
		cls.dealer = make_dealer("Comms Auto-Reply Dealer")

	@classmethod
	def tearDownClass(cls):
		frappe.conf.pop("dms_erp_whatsapp_webhook_secret", None)
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	@patch("dms_erp.comms.api.classify_message")
	def test_replies_and_logs_an_inquiry_when_in_stock(self, mock_classify, mock_resolve, mock_create_inquiry, mock_stock):
		mock_classify.return_value = {"intent": "availability_check", "item_mention": "GVT 6013"}
		mock_resolve.return_value = {"id": "GVT-6013", "name": "Nordic Oak"}
		mock_stock.return_value = 40.0

		comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="GVT 6013 stock hai kya")

		mock_create_inquiry.assert_called_once_with(dealer=self.dealer, item="GVT-6013", qty=1, source="WhatsApp")
		reply = comms_api.last_message(self.dealer)
		self.assertEqual(reply["direction"], "Outbound")
		self.assertIn("Nordic Oak", reply["text"])
		self.assertIn("40", reply["text"])

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	@patch("dms_erp.comms.api.classify_message")
	def test_replies_out_of_stock_when_on_hand_is_zero(self, mock_classify, mock_resolve, mock_create_inquiry, mock_stock):
		mock_classify.return_value = {"intent": "availability_check", "item_mention": "GVT 6013"}
		mock_resolve.return_value = {"id": "GVT-6013", "name": "Nordic Oak"}
		mock_stock.return_value = 0.0

		comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="GVT 6013 available?")

		reply = comms_api.last_message(self.dealer)
		self.assertIn("out of stock", reply["text"])

	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.comms.api.classify_message")
	def test_does_nothing_when_llm_is_not_configured(self, mock_classify, mock_create_inquiry):
		mock_classify.return_value = None

		comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="bhai rate bhejo")

		mock_create_inquiry.assert_not_called()
		self.assertEqual(comms_api.unreplied_inbound_count(self.dealer), 1)

	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.comms.api.classify_message")
	def test_does_nothing_for_a_non_availability_intent(self, mock_classify, mock_create_inquiry):
		mock_classify.return_value = {"intent": "other", "item_mention": None}

		comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="thanks bhai")

		mock_create_inquiry.assert_not_called()

	@patch("dms_erp.catalog.api.resolve_item_mention")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.comms.api.classify_message")
	def test_does_nothing_when_the_item_mention_does_not_resolve(self, mock_classify, mock_create_inquiry, mock_resolve):
		mock_classify.return_value = {"intent": "availability_check", "item_mention": "NO-SUCH-CODE"}
		mock_resolve.return_value = None

		comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="NO-SUCH-CODE available?")

		mock_create_inquiry.assert_not_called()

	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	@patch("dms_erp.comms.api.classify_message")
	def test_does_nothing_when_the_item_is_not_sellable_for_this_dealer(self, mock_classify, mock_resolve, mock_create_inquiry):
		mock_classify.return_value = {"intent": "availability_check", "item_mention": "GVT 6013"}
		mock_resolve.return_value = {"id": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.side_effect = frappe.ValidationError("not sellable")

		# Must not raise, and must not send a reply, even though _create_inquiry rejected it.
		comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="GVT 6013 stock hai kya")
		self.assertEqual(comms_api.unreplied_inbound_count(self.dealer), 1)

	@patch("dms_erp.comms.api.classify_message")
	def test_webhook_still_logs_the_inbound_message_even_if_auto_reply_blows_up(self, mock_classify):
		mock_classify.side_effect = RuntimeError("boom")

		message = comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="GVT 6013 stock hai kya")
		self.assertEqual(message["direction"], "Inbound")
		self.assertEqual(message["status"], "Delivered")


class TestParseSentAt(FrappeTestCase):
	"""Regression coverage for two real bugs found via a live end-to-end test, both on
	the same line: MariaDB's Datetime column rejects an ISO 8601 string outright (a
	SQL error, not a graceful no-op) -- and separately rejects a timezone-*aware*
	datetime just as hard, so parsing alone wasn't the whole fix. whats91 -- like
	WhatsApp Business API generally -- always sends "Z"-suffixed UTC timestamps."""

	def test_returns_now_for_none(self):
		self.assertIsNotNone(comms_api._parse_sent_at(None))

	def test_parses_an_iso_8601_utc_timestamp_as_naive_system_time(self):
		from zoneinfo import ZoneInfo

		from frappe.utils import get_system_timezone

		parsed = comms_api._parse_sent_at("2026-09-26T09:00:00.000Z")

		self.assertIsNone(parsed.tzinfo)  # MariaDB's Datetime column can't store an aware value
		expected = comms_api.get_datetime("2026-09-26T09:00:00.000Z").astimezone(ZoneInfo(get_system_timezone()))
		self.assertEqual(
			(parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute),
			(expected.year, expected.month, expected.day, expected.hour, expected.minute),
		)

	def test_falls_back_to_now_for_an_unparseable_string(self):
		self.assertIsNotNone(comms_api._parse_sent_at("not a real timestamp"))
