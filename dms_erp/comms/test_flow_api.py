from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.comms import api as comms_api
from dms_erp.comms import flow_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer


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
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_with_stock_count_when_in_stock(self, mock_resolve, mock_stock):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_stock.return_value = 40.0

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("Nordic Oak", result["message"])
		self.assertIn("40", result["message"])
		self.assertIn("in stock", result["message"])

		thread = comms_api.list_all_messages(self.dealer)
		self.assertEqual(thread[-2]["direction"], "Inbound")
		self.assertEqual(thread[-2]["text"], "GVT-6013")
		self.assertEqual(thread[-1]["direction"], "Outbound")
		self.assertEqual(thread[-1]["text"], result["message"])

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_out_of_stock_when_on_hand_is_zero(self, mock_resolve, mock_stock):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_stock.return_value = 0.0

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("out of stock", result["message"])

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
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_accepts_lead_as_a_json_string(self, mock_resolve, mock_stock):
		# Frappe's form_dict parsing can hand nested JSON back as a string depending on
		# how the caller posts it -- must not crash on that shape.
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_stock.return_value = 5.0

		result = flow_api.get_item_info(lead='{"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"}')

		self.assertIn("Nordic Oak", result["message"])
