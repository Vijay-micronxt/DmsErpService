from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.comms import api as comms_api
from dms_erp.comms import flow_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer


class TestSplitItemMentions(FrappeTestCase):
	def test_splits_on_comma_and_ampersand_and_newline(self):
		self.assertEqual(flow_api._split_item_mentions("A, B & C\nD"), ["A", "B", "C", "D"])

	def test_splits_on_the_english_and_hindi_word_for_and(self):
		self.assertEqual(flow_api._split_item_mentions("RUSTIC-GREY and Royal Glossy"), ["RUSTIC-GREY", "Royal Glossy"])
		self.assertEqual(flow_api._split_item_mentions("RUSTIC-GREY aur Royal Glossy"), ["RUSTIC-GREY", "Royal Glossy"])
		self.assertEqual(flow_api._split_item_mentions("RUSTIC-GREY और Royal Glossy"), ["RUSTIC-GREY", "Royal Glossy"])

	def test_returns_the_whole_text_as_one_segment_when_no_delimiter_is_present(self):
		self.assertEqual(flow_api._split_item_mentions("GVT-6013"), ["GVT-6013"])


class TestGetItemInfo(FrappeTestCase):
	"""resolve_item_mention/total_stock_for_item have their own tests elsewhere
	(catalog.test_products, warehouse tests) -- these only verify this endpoint's own
	job: resolve the calling dealer from `lead.phone`, log both sides of the exchange
	as a WhatsApp Message (so it shows up in Communications like any other message),
	and reply with the right stock wording."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Flow API Test Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204657")

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_with_stock_count_when_in_stock(self, mock_resolve, mock_create_inquiry, mock_stock):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0001"}
		mock_stock.return_value = 40.0

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("Nordic Oak", result["message"])
		self.assertIn("40", result["message"])
		self.assertIn("in stock", result["message"])
		mock_create_inquiry.assert_called_once_with(dealer=self.dealer, item="GVT-6013", qty=1, source="WhatsApp")

		thread = comms_api.list_all_messages(self.dealer)
		self.assertEqual(thread[-2]["direction"], "Inbound")
		self.assertEqual(thread[-2]["text"], "GVT-6013")
		self.assertEqual(thread[-1]["direction"], "Outbound")
		self.assertEqual(thread[-1]["text"], result["message"])
		self.assertEqual(thread[-1]["relatedType"], "Inquiry")
		self.assertEqual(thread[-1]["relatedRef"], "INQ-0001")

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_out_of_stock_when_on_hand_is_zero(self, mock_resolve, mock_create_inquiry, mock_stock):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0002"}
		mock_stock.return_value = 0.0

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("out of stock", result["message"])
		mock_create_inquiry.assert_called_once_with(dealer=self.dealer, item="GVT-6013", qty=1, source="WhatsApp")

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_still_replies_when_inquiry_creation_is_rejected(self, mock_resolve, mock_create_inquiry, mock_stock):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.side_effect = frappe.PermissionError("not in this dealer's catalog")
		mock_stock.return_value = 10.0

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("in stock", result["message"])
		thread = comms_api.list_all_messages(self.dealer)
		self.assertEqual(thread[-1]["relatedType"], "General")

	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_with_not_found_when_code_does_not_resolve(self, mock_resolve):
		mock_resolve.return_value = None

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "NO-SUCH-CODE"})

		self.assertIn("couldn't find an item", result["message"])
		thread = comms_api.list_all_messages(self.dealer)
		self.assertEqual(thread[-1]["direction"], "Outbound")

	def test_unresolvable_phone_does_not_log_anything_and_replies_safely(self):
		before = len(comms_api.list_all_messages(self.dealer))

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919999999999", "message": "GVT-6013"})

		self.assertIn("dealer account", result["message"])
		self.assertEqual(len(comms_api.list_all_messages(self.dealer)), before)

	def test_blank_message_asks_for_an_item_code_without_logging(self):
		before = len(comms_api.list_all_messages(self.dealer))

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": ""})

		self.assertIn("enter an item code", result["message"])
		self.assertEqual(len(comms_api.list_all_messages(self.dealer)), before)

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_by_name")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_falls_back_to_fuzzy_name_match_when_exact_code_fails(
		self, mock_mention, mock_by_name, mock_create_inquiry, mock_stock
	):
		mock_mention.return_value = None
		mock_by_name.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Royal Glassy"}
		mock_create_inquiry.return_value = {"id": "INQ-0003"}
		mock_stock.return_value = 12.0

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "Royal Glass"})

		mock_by_name.assert_called_once_with(self.dealer, "Royal Glass")
		self.assertIn("Royal Glassy", result["message"])
		self.assertIn("in stock", result["message"])

	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_by_name")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_does_not_try_fuzzy_name_match_when_exact_code_already_resolved(
		self, mock_mention, mock_by_name, mock_create_inquiry
	):
		mock_mention.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0004"}

		with patch("dms_erp.warehouse.utils.total_stock_for_item", return_value=1.0):
			flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		mock_by_name.assert_not_called()

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_accepts_lead_as_a_json_string(self, mock_resolve, mock_create_inquiry, mock_stock):
		# Frappe's form_dict parsing can hand nested JSON back as a string depending on
		# how the caller posts it -- must not crash on that shape.
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0005"}
		mock_stock.return_value = 5.0

		result = flow_api.get_item_info(lead='{"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"}')

		self.assertIn("Nordic Oak", result["message"])

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_handles_a_multi_item_request_as_separate_lookups(self, mock_resolve, mock_create_inquiry, mock_stock):
		items = {
			"RUSTIC-GREY": {"id": "PT-4040-RUSTIC-GREY", "code": "PT-4040-RUSTIC-GREY", "name": "Rustic Grey"},
			"Royal Glossy": {"id": "AAS-001", "code": "AAS-001", "name": "Royal Glossy"},
		}
		mock_resolve.side_effect = lambda dealer, segment: items.get(segment)
		mock_create_inquiry.side_effect = [{"id": "INQ-0006"}, {"id": "INQ-0007"}]
		mock_stock.side_effect = [15.0, 0.0]

		result = flow_api.get_item_info(
			lead={"event": "item.lookup", "phone": "919620204657", "message": "RUSTIC-GREY, Royal Glossy"}
		)

		self.assertEqual(mock_resolve.call_count, 2)
		self.assertIn("Rustic Grey", result["message"])
		self.assertIn("Royal Glossy", result["message"])
		self.assertIn("in stock", result["message"])
		self.assertIn("out of stock", result["message"])
		self.assertEqual(mock_create_inquiry.call_count, 2)

		# Two Inquiries created -- no single reference field can point at both, so
		# the message stays tagged General rather than picking one arbitrarily.
		thread = comms_api.list_all_messages(self.dealer)
		self.assertEqual(thread[-1]["relatedType"], "General")

	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_multi_item_request_reports_each_unresolved_segment_separately(self, mock_resolve):
		mock_resolve.return_value = None

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "FOO-1, BAR-2"})

		self.assertIn("FOO-1", result["message"])
		self.assertIn("BAR-2", result["message"])
