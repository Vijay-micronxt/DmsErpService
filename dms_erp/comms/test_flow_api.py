from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.comms import api as comms_api
from dms_erp.comms import flow_api
from dms_erp.pricing import api as pricing_api
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


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

		# The Flow's own n_set_stock_order_ref node reads this back as
		# erpnext_response.message.item_code to carry the item into "Place Order" --
		# a real production bug (the Flow silently treating a button's own label as
		# the item code) came from this key being missing entirely. Never regress it.
		self.assertEqual(result["item_code"], "GVT-6013")

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
		self.assertIsNone(result["item_code"])
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


class TestGetItemPrice(FrappeTestCase):
	"""Mirrors TestGetItemInfo's structure -- the split/resolve/Inquiry-raising core
	is shared (_resolve_and_track_items, tested via get_item_info above); these only
	verify get_item_price's own job: the custom_price_visible gate and the price
	reply wording."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Flow API Price Test Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204658")
		frappe.db.set_value("Customer", cls.dealer, "custom_price_visible", 1)

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_with_the_dealers_price_when_visible(self, mock_resolve, mock_create_inquiry, mock_price):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0010"}
		mock_price.return_value = 425.5

		result = flow_api.get_item_price(lead={"event": "price.lookup", "phone": "919620204658", "message": "GVT-6013"})

		self.assertIn("Nordic Oak", result["message"])
		self.assertIn("425.5", result["message"])
		mock_price.assert_called_once_with("GVT-6013", self.dealer)
		self.assertEqual(result["item_code"], "GVT-6013")

	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_hides_the_price_when_the_dealers_account_has_it_hidden(self, mock_resolve, mock_create_inquiry):
		frappe.db.set_value("Customer", self.dealer, "custom_price_visible", 0)
		try:
			mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
			mock_create_inquiry.return_value = {"id": "INQ-0011"}

			result = flow_api.get_item_price(lead={"event": "price.lookup", "phone": "919620204658", "message": "GVT-6013"})

			self.assertIn("isn't available", result["message"])
			self.assertNotIn("₹", result["message"])
		finally:
			frappe.db.set_value("Customer", self.dealer, "custom_price_visible", 1)

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_when_no_price_is_published_yet(self, mock_resolve, mock_create_inquiry, mock_price):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0012"}
		mock_price.return_value = None

		result = flow_api.get_item_price(lead={"event": "price.lookup", "phone": "919620204658", "message": "GVT-6013"})

		self.assertIn("no price is published", result["message"])

	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_with_not_found_when_code_does_not_resolve(self, mock_resolve):
		mock_resolve.return_value = None

		result = flow_api.get_item_price(lead={"event": "price.lookup", "phone": "919620204658", "message": "NO-SUCH-CODE"})

		self.assertIn("couldn't find an item", result["message"])
		self.assertIsNone(result["item_code"])

	def test_unresolvable_phone_replies_safely(self):
		result = flow_api.get_item_price(lead={"event": "price.lookup", "phone": "919999999999", "message": "GVT-6013"})

		self.assertIn("dealer account", result["message"])

	def test_blank_message_asks_for_an_item_code(self):
		result = flow_api.get_item_price(lead={"event": "price.lookup", "phone": "919620204658", "message": ""})

		self.assertIn("enter an item code", result["message"])


class TestGetOutstandingDue(FrappeTestCase):
	"""No preceding "wait for input" step in the Flow for this event (it fires
	straight off the main menu button) -- these only verify the reply wording and
	that _outstanding_for is reused rather than duplicated."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Flow API Dues Test Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204659")

	@patch("dms_erp.sales.dealer_portal_api._outstanding_for")
	def test_replies_with_the_outstanding_amount(self, mock_outstanding):
		mock_outstanding.return_value = 1250.75

		result = flow_api.get_outstanding_due(lead={"event": "outstanding.check", "phone": "919620204659"})

		self.assertIn("1,250.75", result["message"])
		mock_outstanding.assert_called_once_with(self.dealer)

	@patch("dms_erp.sales.dealer_portal_api._outstanding_for")
	def test_replies_with_no_dues_when_zero(self, mock_outstanding):
		mock_outstanding.return_value = 0.0

		result = flow_api.get_outstanding_due(lead={"event": "outstanding.check", "phone": "919620204659"})

		self.assertIn("no outstanding dues", result["message"])

	def test_unresolvable_phone_replies_safely(self):
		result = flow_api.get_outstanding_due(lead={"event": "outstanding.check", "phone": "919999999999"})

		self.assertIn("dealer account", result["message"])


class TestGetRecentOrders(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Flow API Recent Orders Test Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204660")

	def test_replies_with_no_orders_when_dealer_has_none(self):
		result = flow_api.get_recent_orders(lead={"event": "orders.recent5", "phone": "919620204660"})

		self.assertIn("don't have any orders", result["message"])

	@patch("dms_erp.comms.flow_api.frappe.get_all")
	def test_lists_the_dealers_recent_orders_with_stage(self, mock_get_all):
		mock_get_all.return_value = [
			frappe._dict({"name": "SAL-ORD-2026-00002", "custom_fulfillment_stage": "Dispatched"}),
			frappe._dict({"name": "SAL-ORD-2026-00001", "custom_fulfillment_stage": None}),
		]

		result = flow_api.get_recent_orders(lead={"event": "orders.recent5", "phone": "919620204660"})

		self.assertIn("SAL-ORD-2026-00002: Dispatched", result["message"])
		self.assertIn("SAL-ORD-2026-00001: Confirmed", result["message"])

	def test_unresolvable_phone_replies_safely(self):
		result = flow_api.get_recent_orders(lead={"event": "orders.recent5", "phone": "919999999999"})

		self.assertIn("dealer account", result["message"])


class TestGetOrderStatus(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.supplier = make_supplier("Flow API Order Status Supplier")
		cls.dealer = make_dealer("Flow API Order Status Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204662")
		cls.item = make_item("FLOW-ORDER-STATUS-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

	def _make_order(self, qty=10):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=qty, source="Phone")
		return order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": qty}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)

	def test_replies_with_the_current_fulfillment_stage(self):
		order = self._make_order()

		result = flow_api.get_order_status(lead={"event": "order.status", "phone": "919620204662", "message": order["number"]})

		self.assertIn(order["number"], result["message"])
		self.assertIn("Confirmed", result["message"])

	def test_replies_with_not_found_for_an_unknown_order_number(self):
		result = flow_api.get_order_status(
			lead={"event": "order.status", "phone": "919620204662", "message": "SAL-ORD-2099-99999"}
		)

		self.assertIn("couldn't find an order", result["message"])

	def test_never_reveals_an_order_belonging_to_a_different_dealer(self):
		order = self._make_order()
		other_dealer = make_dealer("Flow API Order Status Other Dealer")
		frappe.db.set_value("Customer", other_dealer, "custom_phone", "9620204663")

		result = flow_api.get_order_status(lead={"event": "order.status", "phone": "919620204663", "message": order["number"]})

		self.assertIn("couldn't find an order", result["message"])

	def test_unresolvable_phone_replies_safely(self):
		result = flow_api.get_order_status(
			lead={"event": "order.status", "phone": "919999999999", "message": "SAL-ORD-2026-00001"}
		)

		self.assertIn("dealer account", result["message"])

	def test_blank_message_asks_for_an_order_number(self):
		result = flow_api.get_order_status(lead={"event": "order.status", "phone": "919620204662", "message": ""})

		self.assertIn("enter your Sales Order number", result["message"])


class TestGetDeliveryStatus(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.supplier = make_supplier("Flow API Delivery Status Supplier")
		cls.dealer = make_dealer("Flow API Delivery Status Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204664")
		cls.item = make_item("FLOW-DELIVERY-STATUS-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

	def _make_order(self, qty=10):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=qty, source="Phone")
		return order_api.create_order(
			dealer=self.dealer, lines=[{"item": self.item, "qty": qty}], expected_dispatch="2026-09-01", inquiry=inquiry["id"]
		)

	def test_replies_not_yet_dispatched_before_that_stage(self):
		order = self._make_order()

		result = flow_api.get_delivery_status(
			lead={"event": "delivery.status", "phone": "919620204664", "message": order["number"]}
		)

		self.assertIn("hasn't been dispatched yet", result["message"])

	def test_replies_with_the_dispatch_date_once_dispatched(self):
		order = self._make_order()
		frappe.db.set_value("Sales Order", order["number"], "custom_fulfillment_stage", "Dispatched")
		doc = frappe.get_doc("Sales Order", order["number"])
		doc.append(
			"custom_stage_history",
			{"stage": "Dispatched", "at": "2026-09-05 10:00:00", "by": "Administrator", "note": "Test dispatch"},
		)
		doc.save(ignore_permissions=True)

		result = flow_api.get_delivery_status(
			lead={"event": "delivery.status", "phone": "919620204664", "message": order["number"]}
		)

		self.assertIn("dispatched", result["message"].lower())
		self.assertIn("05 Sep 2026", result["message"])

	def test_replies_with_not_found_for_an_unknown_order_number(self):
		result = flow_api.get_delivery_status(
			lead={"event": "delivery.status", "phone": "919620204664", "message": "SAL-ORD-2099-99999"}
		)

		self.assertIn("couldn't find an order", result["message"])


class TestLeadVariables(FrappeTestCase):
	def test_parses_variables_given_as_a_json_string(self):
		lead = {"variables": '{"order_qty_band": "10 - 50 units"}'}
		self.assertEqual(flow_api._lead_variables(lead), {"order_qty_band": "10 - 50 units"})

	def test_returns_empty_dict_for_missing_or_malformed_variables(self):
		self.assertEqual(flow_api._lead_variables({}), {})
		self.assertEqual(flow_api._lead_variables({"variables": "not json"}), {})
		self.assertEqual(flow_api._lead_variables({"variables": None}), {})

	def test_lead_itself_as_a_json_string_still_works(self):
		lead = '{"phone": "919620204657", "variables": {"order_item_code": "GVT-6013"}}'
		self.assertEqual(flow_api._lead_variables(lead), {"order_item_code": "GVT-6013"})


class TestCreateDealerOpportunity(FrappeTestCase):
	"""opportunity.create is used for both "Request More Info" (no PO number in
	lead.variables -- raises an Inquiry) and "Place Order" (a PO number present --
	creates a real Sales Order per BRD C.2.2). The Flow doesn't collect a PO number
	anywhere yet, so in practice only the Inquiry branch fires today; both are
	tested here so the Order branch is already correct once the Flow is updated."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Flow API Opportunity Test Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204665")

	def test_unresolvable_phone_replies_safely(self):
		result = flow_api.create_dealer_opportunity(
			lead={"event": "opportunity.create", "phone": "919999999999", "variables": {"order_item_code": "GVT-6013"}}
		)

		self.assertIn("dealer account", result["message"])

	def test_replies_safely_when_no_item_code_is_available(self):
		result = flow_api.create_dealer_opportunity(
			lead={"event": "opportunity.create", "phone": "919620204665", "variables": {}}
		)

		self.assertIn("couldn't tell which item", result["message"])

	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	def test_raises_an_inquiry_when_no_po_number_is_present(self, mock_create_inquiry):
		mock_create_inquiry.return_value = {"id": "INQ-0020"}

		result = flow_api.create_dealer_opportunity(
			lead={
				"event": "opportunity.create",
				"phone": "919620204665",
				"message": "Need 500 units by end of month",
				"variables": {"order_item_code": "GVT-6013"},
			}
		)

		mock_create_inquiry.assert_called_once_with(
			dealer=self.dealer, item="GVT-6013", qty=1, source="WhatsApp", remarks="Need 500 units by end of month"
		)
		self.assertIn("noted your enquiry", result["message"])

	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	def test_inquiry_rejection_still_replies_safely(self, mock_create_inquiry):
		mock_create_inquiry.side_effect = frappe.PermissionError("not in catalog")

		result = flow_api.create_dealer_opportunity(
			lead={"event": "opportunity.create", "phone": "919620204665", "variables": {"order_item_code": "GVT-6013"}}
		)

		self.assertIn("couldn't raise this enquiry", result["message"])

	@patch("dms_erp.sales.order_api._create_order")
	def test_places_a_real_order_when_a_po_number_is_present(self, mock_create_order):
		mock_create_order.return_value = {"id": "SAL-ORD-2026-00099", "number": "SAL-ORD-2026-00099"}

		result = flow_api.create_dealer_opportunity(
			lead={
				"event": "opportunity.create",
				"phone": "919620204665",
				"variables": {
					"order_item_code": "GVT-6013",
					"order_qty_band": "100 - 200 units",
					"order_note": "Urgent - please prioritize",
					"po_number": "PO-1234",
				},
			}
		)

		mock_create_order.assert_called_once()
		_args, kwargs = mock_create_order.call_args
		self.assertEqual(kwargs["dealer"], self.dealer)
		self.assertEqual(kwargs["lines"], [{"item": "GVT-6013", "qty": 150}])
		self.assertEqual(kwargs["customer_po"], "PO-1234")
		self.assertIn("SAL-ORD-2026-00099", result["message"])
		self.assertIn("PO-1234", result["message"])

	@patch("dms_erp.sales.order_api._create_order")
	def test_order_creation_failure_replies_safely(self, mock_create_order):
		mock_create_order.side_effect = frappe.ValidationError("GVT-6013 has no approved dealer price yet.")

		result = flow_api.create_dealer_opportunity(
			lead={
				"event": "opportunity.create",
				"phone": "919620204665",
				"variables": {"order_item_code": "GVT-6013", "po_number": "PO-1234"},
			}
		)

		self.assertIn("couldn't place this order", result["message"])
