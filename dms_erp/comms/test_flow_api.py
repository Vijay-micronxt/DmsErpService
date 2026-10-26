from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.comms import api as comms_api
from dms_erp.comms import flow_api
from dms_erp.pricing import api as pricing_api
from dms_erp.sales import inquiry_api, order_api
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_dealer_addresses,
	make_item,
	make_supplier,
)
from dms_erp.warehouse.utils import default_company


class TestSplitItemMentions(FrappeTestCase):
	def test_splits_on_comma_and_ampersand_and_newline(self):
		self.assertEqual(flow_api._split_item_mentions("A, B & C\nD"), ["A", "B", "C", "D"])

	def test_splits_on_the_english_and_hindi_word_for_and(self):
		self.assertEqual(flow_api._split_item_mentions("RUSTIC-GREY and Royal Glossy"), ["RUSTIC-GREY", "Royal Glossy"])
		self.assertEqual(flow_api._split_item_mentions("RUSTIC-GREY aur Royal Glossy"), ["RUSTIC-GREY", "Royal Glossy"])
		self.assertEqual(flow_api._split_item_mentions("RUSTIC-GREY और Royal Glossy"), ["RUSTIC-GREY", "Royal Glossy"])

	def test_returns_the_whole_text_as_one_segment_when_no_delimiter_is_present(self):
		self.assertEqual(flow_api._split_item_mentions("GVT-6013"), ["GVT-6013"])


class TestExtractQty(FrappeTestCase):
	"""BRD C.2.2's own sample conversation folds a quantity into the same message as
	the item code ("White tiles 40 boxes available?") -- an explicit unit word is
	required so an item code that itself ends in digits (GVT-6013, AAS-001) is never
	misread as carrying a quantity."""

	def test_extracts_a_quantity_with_an_explicit_unit_word(self):
		self.assertEqual(flow_api._extract_qty("GVT-6013 40 boxes"), ("GVT-6013", 40.0))
		self.assertEqual(flow_api._extract_qty("Nordic Oak 15 pcs"), ("Nordic Oak", 15.0))
		self.assertEqual(flow_api._extract_qty("White tiles 40 boxes"), ("White tiles", 40.0))

	def test_leaves_a_bare_item_code_untouched(self):
		self.assertEqual(flow_api._extract_qty("GVT-6013"), ("GVT-6013", None))
		self.assertEqual(flow_api._extract_qty("AAS-001"), ("AAS-001", None))

	def test_does_not_treat_bare_trailing_digits_as_a_quantity(self):
		# No unit word -- "GVT-6013 40" is left completely alone rather than guessing
		# whether "40" is a quantity or just part of what the dealer typed.
		self.assertEqual(flow_api._extract_qty("GVT-6013 40"), ("GVT-6013 40", None))

	def test_extracts_a_bracketed_quantity(self):
		# WhatsApp Inquiry gap #6 -- "Either accept all sort of formats like BCH(220 box)".
		self.assertEqual(flow_api._extract_qty("BCH(220 box)"), ("BCH", 220.0))
		self.assertEqual(flow_api._extract_qty("BCH (220 boxes)"), ("BCH", 220.0))
		self.assertEqual(flow_api._extract_qty("BCH [220 boxes]"), ("BCH", 220.0))
		self.assertEqual(flow_api._extract_qty("BCH(220box)."), ("BCH", 220.0))

	def test_tolerates_trailing_punctuation(self):
		self.assertEqual(flow_api._extract_qty("White tiles 40 boxes?"), ("White tiles", 40.0))


class TestGetItemInfo(FrappeTestCase):
	"""resolve_item_mention/total_stock_for_item have their own tests elsewhere
	(catalog.test_products, warehouse tests) -- these only verify this endpoint's own
	job: resolve the calling dealer from `lead.phone`, log both sides of the exchange
	as a WhatsApp Message (so it shows up in Communications like any other message),
	and reply with the right stock wording. No price, ever: it depends on delivery
	and other details only a quotation settles (client request, Oct 2026)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Flow API Test Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204657")
		frappe.db.set_value("Customer", cls.dealer, "custom_price_visible", 1)

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_with_stock_and_never_a_price_when_in_stock(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_price
	):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0001"}
		mock_stock.return_value = 40.0
		mock_price.return_value = 425.5

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("Nordic Oak", result["message"])
		self.assertIn("40", result["message"])
		self.assertIn("in stock", result["message"])
		self.assertNotIn("425.5", result["message"])
		self.assertNotIn("₹", result["message"])
		mock_price.assert_not_called()
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

	@patch("dms_erp.catalog.api.resolve_item_by_name")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_a_word_fitting_several_items_lists_them_numbered_to_pick_from(self, mock_mention, mock_by_name):
		mock_mention.return_value = None
		mock_by_name.return_value = {
			"status": "ambiguous",
			"total": 11,
			"candidates": [{"name": f"Royal Tile {i}", "code": f"RT-{i}"} for i in range(1, 8)]
			# a Tally-migrated item: the name and the code are the same text
			+ [{"name": "Royal Plain 600x600", "code": "Royal Plain 600x600"}],
		}

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "royal"})

		self.assertIn("We found 11 items matching 'royal'", result["message"])
		self.assertIn("1. Royal Tile 1 (RT-1)", result["message"])
		self.assertIn("8. Royal Plain 600x600\n", result["message"])
		self.assertNotIn("Royal Plain 600x600 (Royal Plain 600x600)", result["message"])
		self.assertIn("…and 3 more.", result["message"])
		self.assertIn("type more of the name to narrow it down", result["message"])
		self.assertIsNone(result["item_code"])

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_never_includes_a_price_even_when_the_dealers_account_shows_prices(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_price
	):
		# custom_price_visible is 1 for this dealer (setUpClass) and a price exists --
		# the old per-dealer switch no longer matters: no price before a quotation.
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0013"}
		mock_stock.return_value = 40.0
		mock_price.return_value = 425.5

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("in stock", result["message"])
		self.assertNotIn("₹", result["message"])
		self.assertNotIn("425", result["message"])
		mock_price.assert_not_called()

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_includes_size_and_finish_when_available(self, mock_resolve, mock_create_inquiry, mock_stock, mock_price):
		mock_resolve.return_value = {
			"id": "GVT-6013",
			"code": "GVT-6013",
			"name": "Nordic Oak",
			"size": "600x1200",
			"finish": "Glossy",
		}
		mock_create_inquiry.return_value = {"id": "INQ-0015"}
		mock_stock.return_value = 40.0
		mock_price.return_value = None

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("600x1200", result["message"])
		self.assertIn("Glossy", result["message"])

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.suggest_batch_combination")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_names_the_specific_batch_when_a_quantity_is_given_and_one_batch_suffices(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_combo, mock_price
	):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0020"}
		mock_stock.return_value = 45.0
		mock_combo.return_value = {
			"sufficient": True,
			"batches": [{"batchNumber": "A030", "itemCode": "GVT-6013", "itemName": "Nordic Oak", "boxes": 25.0}],
			"totalBoxes": 25.0,
		}
		mock_price.return_value = None

		result = flow_api.get_item_info(
			lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013 20 boxes"}
		)

		mock_combo.assert_called_once_with("GVT-6013", 20.0)
		self.assertIn("Batch A030 (25)", result["message"])
		self.assertIn("your 20 boxes", result["message"])
		# The quantity the dealer actually asked for feeds the Inquiry's own qty too,
		# not the hardcoded placeholder used when no quantity was given.
		mock_create_inquiry.assert_called_once_with(dealer=self.dealer, item="GVT-6013", qty=20.0, source="WhatsApp")

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.suggest_batch_combination")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_names_a_batch_combination_when_no_single_batch_suffices(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_combo, mock_price
	):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0021"}
		mock_stock.return_value = 45.0
		mock_combo.return_value = {
			"sufficient": True,
			"batches": [
				{"batchNumber": "A030", "itemCode": "GVT-6013", "itemName": "Nordic Oak", "boxes": 25.0},
				{"batchNumber": "B012", "itemCode": "GVT-6013", "itemName": "Nordic Oak", "boxes": 20.0},
			],
			"totalBoxes": 45.0,
		}
		mock_price.return_value = None

		result = flow_api.get_item_info(
			lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013 40 boxes"}
		)

		self.assertIn("Batch A030 (25) + Batch B012 (20)", result["message"])
		self.assertIn("45 boxes total", result["message"])

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.suggest_batch_combination")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_reports_a_shortfall_when_even_the_top_batches_dont_cover_the_quantity(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_combo, mock_price
	):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0022"}
		mock_stock.return_value = 45.0
		mock_combo.return_value = {
			"sufficient": False,
			"batches": [{"batchNumber": "A030", "itemCode": "GVT-6013", "itemName": "Nordic Oak", "boxes": 45.0}],
			"totalBoxes": 45.0,
		}
		mock_price.return_value = None

		result = flow_api.get_item_info(
			lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013 100 boxes"}
		)

		self.assertIn("only has 45 boxes", result["message"])
		self.assertIn("short of your 100", result["message"])

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.suggest_batch_combination")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_quantity_in_the_message_does_not_prevent_the_item_from_resolving(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_combo, mock_price
	):
		# The item resolver must see the code with the quantity already stripped off,
		# not "GVT-6013 40 boxes" as one blob.
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0023"}
		mock_stock.return_value = 45.0
		mock_combo.return_value = {"sufficient": True, "batches": [], "totalBoxes": 0.0}
		mock_price.return_value = None

		flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013 40 boxes"})

		mock_resolve.assert_called_once_with(self.dealer, "GVT-6013")

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.suggest_batch_combination")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_falls_back_to_the_flat_total_when_quantity_is_given_but_no_batch_lots_exist(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_combo, mock_price
	):
		# A data inconsistency (real Bin stock with no matching Batch lots) must never
		# produce a broken "we suggest  -- 0 boxes total" reply.
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0024"}
		mock_stock.return_value = 45.0
		mock_combo.return_value = {"sufficient": False, "batches": [], "totalBoxes": 0.0}
		mock_price.return_value = None

		result = flow_api.get_item_info(
			lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013 40 boxes"}
		)

		self.assertIn("is in stock — 45 boxes available", result["message"])

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.all_batches")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_lists_every_batch_when_no_quantity_is_given_and_several_batches_exist(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_all_batches, mock_price
	):
		# Gap #2 (WhatsApp Inquiry sheet): "if multiple batches are available, all
		# the batches should list down with the individual quantities" -- a bare
		# "is it in stock" with no quantity typed must no longer collapse several
		# real batches into one flat total.
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0030"}
		mock_stock.return_value = 70.0
		mock_all_batches.return_value = [
			{"batchNumber": "A030", "itemCode": "GVT-6013", "itemName": "Nordic Oak", "boxes": 45.0},
			{"batchNumber": "A031", "itemCode": "GVT-6013", "itemName": "Nordic Oak", "boxes": 25.0},
		]
		mock_price.return_value = None

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("Batch A030 (45)", result["message"])
		self.assertIn("Batch A031 (25)", result["message"])
		self.assertIn("70 boxes total", result["message"])

	@patch("dms_erp.warehouse.utils.all_batches")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_single_batch_is_named_too(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_all_batches
	):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0031"}
		mock_stock.return_value = 45.0
		mock_all_batches.return_value = [{"batchNumber": "A030", "itemCode": "GVT-6013", "itemName": "Nordic Oak", "boxes": 45.0}]

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("is in stock — Batch A030 (45 boxes)", result["message"])

	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_replies_out_of_stock_when_on_hand_is_zero(self, mock_resolve, mock_create_inquiry, mock_stock):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0002"}
		mock_stock.return_value = 0.0

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("out of stock", result["message"])

	@patch("dms_erp.catalog.dealer_catalog_api.catalog_for")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_out_of_stock_reply_includes_lead_time_and_a_real_alternative(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_catalog_for
	):
		alt_code = make_item("FLOW-ALT-ITEM", "Vitrified")
		frappe.db.set_value("Item", alt_code, "item_name", "Nordic Ash")

		mock_resolve.return_value = {
			"id": "GVT-6013",
			"code": "GVT-6013",
			"name": "Nordic Oak",
			"leadTimeDays": 14,
			"altItemId": alt_code,
		}
		mock_create_inquiry.return_value = {"id": "INQ-0016"}
		mock_stock.side_effect = [0.0, 15.0]  # main item out of stock, alternative in stock
		mock_catalog_for.return_value = ["GVT-6013", alt_code]

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertIn("out of stock", result["message"])
		self.assertIn("14 day", result["message"])
		self.assertIn("Nordic Ash", result["message"])
		self.assertIn(alt_code, result["message"])

	@patch("dms_erp.catalog.dealer_catalog_api.catalog_for")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_alternative_omitted_when_outside_the_dealers_catalog(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_catalog_for
	):
		mock_resolve.return_value = {
			"id": "GVT-6013",
			"code": "GVT-6013",
			"name": "Nordic Oak",
			"altItemId": "GVT-6020",
		}
		mock_create_inquiry.return_value = {"id": "INQ-0017"}
		mock_stock.return_value = 0.0
		mock_catalog_for.return_value = ["GVT-6013"]  # GVT-6020 not assigned to this dealer

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertNotIn("consider", result["message"])

	@patch("dms_erp.catalog.dealer_catalog_api.catalog_for")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_alternative_omitted_when_it_is_also_out_of_stock(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_catalog_for
	):
		mock_resolve.return_value = {
			"id": "GVT-6013",
			"code": "GVT-6013",
			"name": "Nordic Oak",
			"altItemId": "GVT-6020",
		}
		mock_create_inquiry.return_value = {"id": "INQ-0018"}
		mock_stock.side_effect = [0.0, 0.0]  # both the item and its alternative are out of stock
		mock_catalog_for.return_value = ["GVT-6013", "GVT-6020"]

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		self.assertNotIn("consider", result["message"])
		mock_create_inquiry.assert_called_once_with(dealer=self.dealer, item="GVT-6013", qty=1, source="WhatsApp")

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_still_replies_when_inquiry_creation_is_rejected(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_price
	):
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.side_effect = frappe.PermissionError("not in this dealer's catalog")
		mock_stock.return_value = 10.0
		mock_price.return_value = None

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

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_by_name")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_falls_back_to_fuzzy_name_match_when_exact_code_fails(
		self, mock_mention, mock_by_name, mock_create_inquiry, mock_stock, mock_price
	):
		mock_mention.return_value = None
		mock_by_name.return_value = {
			"status": "matched",
			"item": {"id": "GVT-6013", "code": "GVT-6013", "name": "Royal Glassy"},
		}
		mock_create_inquiry.return_value = {"id": "INQ-0003"}
		mock_stock.return_value = 12.0
		mock_price.return_value = None

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "Royal Glass"})

		mock_by_name.assert_called_once_with(self.dealer, "Royal Glass")
		self.assertIn("Royal Glassy", result["message"])
		self.assertIn("in stock", result["message"])

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_by_name")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_does_not_try_fuzzy_name_match_when_exact_code_already_resolved(
		self, mock_mention, mock_by_name, mock_create_inquiry, mock_price
	):
		mock_mention.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0004"}
		mock_price.return_value = None

		with patch("dms_erp.warehouse.utils.total_stock_for_item", return_value=1.0):
			flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"})

		mock_by_name.assert_not_called()

	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_by_name")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_ambiguous_name_match_asks_to_confirm_instead_of_guessing(
		self, mock_mention, mock_by_name, mock_create_inquiry
	):
		# BRD C.2.1: "on a mismatch or multiple candidates ... prompted to confirm the
		# exact code rather than the system guessing." Never resolve to either
		# candidate silently -- this is exactly the "one finish/sub-type mistaken for
		# another" scenario the BRD names.
		mock_mention.return_value = None
		mock_by_name.return_value = {
			"status": "ambiguous",
			"candidates": [
				{"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak Glossy"},
				{"id": "GVT-6014", "code": "GVT-6014", "name": "Nordic Oak Matte"},
			],
		}

		result = flow_api.get_item_info(lead={"event": "item.lookup", "phone": "919620204657", "message": "Nordic Oak"})

		self.assertIn("Nordic Oak Glossy", result["message"])
		self.assertIn("GVT-6013", result["message"])
		self.assertIn("Nordic Oak Matte", result["message"])
		self.assertIn("GVT-6014", result["message"])
		self.assertIn("exact item code", result["message"])
		# No confirmed item -- neither an Inquiry raised nor item_code carried
		# forward into "🛒 Place Order" (which would otherwise silently place an
		# order for whichever candidate happened to be resolved first).
		mock_create_inquiry.assert_not_called()
		self.assertIsNone(result["item_code"])

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_accepts_lead_as_a_json_string(self, mock_resolve, mock_create_inquiry, mock_stock, mock_price):
		# Frappe's form_dict parsing can hand nested JSON back as a string depending on
		# how the caller posts it -- must not crash on that shape.
		mock_resolve.return_value = {"id": "GVT-6013", "code": "GVT-6013", "name": "Nordic Oak"}
		mock_create_inquiry.return_value = {"id": "INQ-0005"}
		mock_stock.return_value = 5.0
		mock_price.return_value = None

		result = flow_api.get_item_info(lead='{"event": "item.lookup", "phone": "919620204657", "message": "GVT-6013"}')

		self.assertIn("Nordic Oak", result["message"])

	@patch("dms_erp.pricing.api.get_price_for_dealer")
	@patch("dms_erp.warehouse.utils.total_stock_for_item")
	@patch("dms_erp.sales.inquiry_api._create_inquiry")
	@patch("dms_erp.catalog.api.resolve_item_mention")
	def test_handles_a_multi_item_request_as_separate_lookups(
		self, mock_resolve, mock_create_inquiry, mock_stock, mock_price
	):
		items = {
			"RUSTIC-GREY": {"id": "PT-4040-RUSTIC-GREY", "code": "PT-4040-RUSTIC-GREY", "name": "Rustic Grey"},
			"Royal Glossy": {"id": "AAS-001", "code": "AAS-001", "name": "Royal Glossy"},
		}
		mock_resolve.side_effect = lambda dealer, segment: items.get(segment)
		mock_create_inquiry.side_effect = [{"id": "INQ-0006"}, {"id": "INQ-0007"}]
		mock_stock.side_effect = [15.0, 0.0]
		mock_price.return_value = None

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
		cls.billing_address, cls.shipping_address = make_dealer_addresses(cls.dealer)
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204662")
		cls.item = make_item("FLOW-ORDER-STATUS-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

	def _make_order(self, qty=10):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=qty, source="Phone")
		return order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": qty}],
			expected_dispatch="2026-09-01",
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
			inquiry=inquiry["id"],
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
		cls.billing_address, cls.shipping_address = make_dealer_addresses(cls.dealer)
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204664")
		cls.item = make_item("FLOW-DELIVERY-STATUS-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

	def _make_order(self, qty=10):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=qty, source="Phone")
		return order_api.create_order(
			dealer=self.dealer,
			lines=[{"item": self.item, "qty": qty}],
			expected_dispatch="2026-09-01",
			customer_address=self.billing_address,
			shipping_address_name=self.shipping_address,
			inquiry=inquiry["id"],
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

	@patch("dms_erp.sales.order_api._create_order")
	def test_order_creation_failure_never_leaks_erpnext_native_html_or_emails(self, mock_create_order):
		# The real report this guards against: ERPNext's own native Sales Order
		# credit-limit check throws an HTML-formatted message naming internal
		# Credit Controller users and their emails -- forwarded verbatim, a
		# WhatsApp dealer saw exactly this, tags and all.
		mock_create_order.side_effect = frappe.ValidationError(
			"Credit limit has been crossed for customer Medha - 1(37290.0/1000.0)<br><br>"
			"Please contact any of the following users to extend the credit limits for Medha - 1: "
			"<br><br> <ul><li>Karthik Mxt (karthikeyan@micronxt.com)</li></ul>."
		)

		result = flow_api.create_dealer_opportunity(
			lead={
				"event": "opportunity.create",
				"phone": "919620204665",
				"variables": {"order_item_code": "GVT-6013", "po_number": "PO-1234"},
			}
		)

		self.assertNotIn("<", result["message"])
		self.assertNotIn("micronxt.com", result["message"])
		self.assertNotIn("Karthik", result["message"])
		self.assertIn("Credit limit has been crossed for customer Medha - 1", result["message"])
		self.assertIn("couldn't place this order", result["message"])

	@patch("dms_erp.sales.order_api._create_order")
	def test_order_over_credit_limit_is_queued_not_placed(self, mock_create_order):
		item = make_item("FLOW-CREDIT-ITEM", "Vitrified")
		supplier = make_supplier("Flow API Credit Test Supplier")
		pricing_api.ensure_price_record(item, supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")

		dealer = make_dealer("Flow API Credit Limit Dealer")
		frappe.db.set_value("Customer", dealer, "custom_phone", "919888888888")
		doc = frappe.get_doc("Customer", dealer)
		doc.append("credit_limits", {"company": default_company(), "credit_limit": 1000})
		doc.save(ignore_permissions=True)

		result = flow_api.create_dealer_opportunity(
			lead={
				"event": "opportunity.create",
				"phone": "919888888888",
				"variables": {
					"order_item_code": item,
					"order_qty_band": "100 - 200 units",  # midpoint 150 -> 150*500 = 75000, over the 1000 limit
					"po_number": "PO-CREDIT-1",
				},
			}
		)

		mock_create_order.assert_not_called()
		self.assertIn("credit limit", result["message"].lower())
		self.assertNotIn("<", result["message"])

	@patch("dms_erp.sales.order_api._create_order")
	def test_place_order_on_an_unassigned_item_raises_an_inquiry_instead(self, mock_create_order):
		# ISS-2026-00644: a dealer can look up/inquire on an item outside their own
		# Dealer Catalog assignment over WhatsApp (resolve_item_by_name is now scoped
		# to browsable_catalog_for), but the Place-Order step must never actually
		# book it -- same assigned/orderable boundary dealer_portal_api.convert_to_order
		# enforces for the portal.
		from dms_erp.catalog.dealer_catalog_api import set_product_visibility

		item = make_item("FLOW-UNASSIGNED-ITEM", "Vitrified")
		other_item = make_item("FLOW-ASSIGNED-ITEM", "Vitrified")
		dealer = make_dealer("Flow API Unassigned Item Dealer")
		frappe.db.set_value("Customer", dealer, "custom_phone", "919777777777")
		# Assigning anything at all creates dealer's Dealer Catalog record, taking it
		# off the no-record-at-all full-access fallback every other test here relies on.
		set_product_visibility(dealer, other_item, True)

		result = flow_api.create_dealer_opportunity(
			lead={
				"event": "opportunity.create",
				"phone": "919777777777",
				"variables": {"order_item_code": item, "po_number": "PO-UNASSIGNED-1"},
			}
		)

		mock_create_order.assert_not_called()
		self.assertIn("isn't yet set up for ordering", result["message"])


class TestCreateCustomStyleInquiry(FrappeTestCase):
	"""custom_style.create -- the new "🎨 Custom Style" WhatsApp menu option
	(MOM 3.6 / ISS-2026-00636): a design not in the item master at all, logged as
	a genuine Inquiry (sales.inquiry_api._create_inquiry's `requested_style`),
	never an ERPNext Opportunity."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Flow API Custom Style Test Dealer")
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "919666666666")

	def test_unresolvable_phone_replies_safely(self):
		result = flow_api.create_custom_style_inquiry(
			lead={"phone": "919999999998", "message": "Carrara Marble Look"}
		)
		self.assertIn("dealer account", result["message"])

	def test_replies_safely_when_no_description_is_given(self):
		result = flow_api.create_custom_style_inquiry(lead={"phone": "919666666666", "message": ""})
		self.assertIn("describe the style", result["message"].lower())

	def test_logs_a_genuine_inquiry_with_no_item_and_no_opportunity(self):
		result = flow_api.create_custom_style_inquiry(
			lead={"phone": "919666666666", "message": "Carrara Marble Look, 600x1200"}
		)
		self.assertIn("noted your custom style request", result["message"])

		from dms_erp.sales.inquiry_api import list_all_inquiries

		inquiries = [i for i in list_all_inquiries(dealer=self.dealer) if i["requestedStyle"]]
		self.assertEqual(len(inquiries), 1)
		self.assertIsNone(inquiries[0]["productId"])
		self.assertEqual(inquiries[0]["requestedStyle"], "Carrara Marble Look, 600x1200")
		self.assertEqual(inquiries[0]["source"], "WhatsApp")
		self.assertIsNone(inquiries[0]["linkedOpportunity"])


class TestWhatsAppQuotationBeforeOrder(FrappeTestCase):
	"""Place Order no longer goes straight from quantity/note/PO to a Sales Order: the
	bot first shows a real quotation (create_whatsapp_quotation) and the PO number step
	then confirms THAT quotation (create_dealer_opportunity with quotation_id)."""

	PHONE = "919620204777"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from dms_erp.catalog import dealer_catalog_api
		from dms_erp.catalog.setup import setup_catalog
		from dms_erp.pricing import api as pricing_api
		from dms_erp.pricing.setup import setup_pricing
		from dms_erp.warehouse.test_fixtures import make_item, make_supplier

		ensure_company()
		setup_catalog()
		setup_pricing()
		supplier = make_supplier("WA Quotation Test Supplier")
		cls.dealer = make_dealer("WA Quotation Test Dealer")
		make_dealer_addresses(cls.dealer)
		frappe.db.set_value("Customer", cls.dealer, "custom_phone", "9620204777")
		cls.other_dealer = make_dealer("WA Quotation Other Dealer")
		make_dealer_addresses(cls.other_dealer)
		frappe.db.set_value("Customer", cls.other_dealer, "custom_phone", "9620204778")

		cls.item = make_item("WAQ-PRICED", "Vitrified")
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.item, True)
		dealer_catalog_api.set_product_visibility(cls.other_dealer, cls.item, True)
		pricing_api.ensure_price_record(cls.item, supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

		cls.unpriced_item = make_item("WAQ-UNPRICED", "Vitrified")
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.unpriced_item, True)

		cls.unassigned_item = make_item("WAQ-UNASSIGNED", "Vitrified")

	def _lead(self, item, qty="220", message="No note", phone=None, **extra):
		variables = {"order_item_code": item, "order_qty_band": qty, "order_note": "No note", **extra}
		return {"event": "quotation.create", "phone": phone or self.PHONE, "message": message, "variables": variables}

	def test_shows_a_real_quotation_for_the_typed_quantity(self):
		result = flow_api.create_whatsapp_quotation(lead=self._lead(self.item, qty="220"))

		self.assertTrue(result["quotation_id"])
		self.assertIn(result["quotation_id"], result["message"])
		self.assertIn(self.item, result["message"])
		self.assertIn("220 boxes", result["message"])
		self.assertIn("₹360", result["message"])  # the dealer's own tier rate, no markup
		self.assertIn("₹79,200", result["message"])  # 220 x 360
		self.assertIn("GST and freight extra", result["message"])

		quotation = frappe.get_doc("Quotation", result["quotation_id"])
		self.assertEqual(quotation.party_name, self.dealer)
		self.assertEqual(quotation.docstatus, 1)
		self.assertEqual((quotation.items[0].item_code, quotation.items[0].qty, quotation.items[0].rate), (self.item, 220, 360))
		# An Inquiry for the quantity actually asked is logged against it.
		inquiry = frappe.db.get_value("Inquiry", {"linked_quotation": quotation.name}, ["qty", "status", "source"], as_dict=True)
		self.assertEqual((inquiry.qty, inquiry.status, inquiry.source), (220, "Quoted", "WhatsApp"))

	@patch("dms_erp.warehouse.utils.suggest_batch_combination")
	def test_the_quotation_names_the_batches_the_quantity_would_come_from(self, mock_combo):
		mock_combo.return_value = {
			"sufficient": True,
			"totalBoxes": 220.0,
			"batches": [
				{"batchNumber": "A030", "boxes": 150.0},
				{"batchNumber": "A031", "boxes": 70.0},
			],
		}
		result = flow_api.create_whatsapp_quotation(lead=self._lead(self.item, qty="220"))
		self.assertIn("Suggested Batch A030 (150) + Batch A031 (70)", result["message"])

		mock_combo.return_value = {"sufficient": False, "totalBoxes": 90.0, "batches": [{"batchNumber": "A030", "boxes": 90.0}]}
		short = flow_api.create_whatsapp_quotation(lead=self._lead(self.item, qty="220"))
		self.assertIn("In stock: Batch A030 (90) — 90 of 220 boxes", short["message"])

	def test_a_range_label_quotes_its_midpoint(self):
		result = flow_api.create_whatsapp_quotation(lead=self._lead(self.item, qty="50 - 100 units"))
		self.assertTrue(result["quotation_id"])
		self.assertEqual(frappe.get_doc("Quotation", result["quotation_id"]).items[0].qty, flow_api._QTY_BAND_MIDPOINT["50 - 100 units"])

	def test_an_item_outside_the_dealers_catalog_gets_an_enquiry_not_a_quotation(self):
		result = flow_api.create_whatsapp_quotation(lead=self._lead(self.unassigned_item))
		self.assertEqual(result["quotation_id"], "")
		self.assertIn("isn't yet set up for ordering", result["message"])
		inquiry = frappe.db.get_value("Inquiry", {"dealer": self.dealer, "item": self.unassigned_item}, "linked_quotation")
		self.assertFalse(inquiry)  # logged as an enquiry, never quoted

	def test_an_item_with_no_approved_price_gets_an_enquiry_not_a_quotation(self):
		result = flow_api.create_whatsapp_quotation(lead=self._lead(self.unpriced_item))
		self.assertEqual(result["quotation_id"], "")
		self.assertIn("can't prepare a quotation", result["message"])
		self.assertTrue(frappe.db.exists("Inquiry", {"dealer": self.dealer, "item": self.unpriced_item}))

	def test_unresolvable_phone_and_missing_item_reply_safely(self):
		self.assertEqual(flow_api.create_whatsapp_quotation(lead=self._lead(self.item, phone="919999999990"))["quotation_id"], "")
		missing = flow_api.create_whatsapp_quotation(lead={"phone": self.PHONE, "message": "x", "variables": {}})
		self.assertEqual(missing["quotation_id"], "")
		self.assertIn("couldn't tell which item", missing["message"])

	def test_confirming_with_a_po_number_places_the_order_from_that_quotation(self):
		quoted = flow_api.create_whatsapp_quotation(lead=self._lead(self.item, qty="40"))
		qid = quoted["quotation_id"]

		result = flow_api.create_dealer_opportunity(
			lead=self._lead(self.item, qty="40", message="PO-WA-1", po_number="PO-WA-1", quotation_id=qid)
		)

		self.assertIn("has been placed against your PO PO-WA-1", result["message"])
		self.assertIn(qid, result["message"])
		order = frappe.get_doc("Sales Order", {"po_no": "PO-WA-1"})
		self.assertEqual(order.customer, self.dealer)
		self.assertEqual((order.items[0].item_code, order.items[0].qty, order.items[0].rate), (self.item, 40, 360))
		self.assertEqual(frappe.db.get_value("Quotation", qid, "status"), "Ordered")
		self.assertEqual(frappe.db.get_value("Inquiry", {"linked_quotation": qid}, "status"), "Converted to Order")

	def test_another_dealers_quotation_cannot_be_confirmed(self):
		quoted = flow_api.create_whatsapp_quotation(lead=self._lead(self.item, qty="10"))
		result = flow_api.create_dealer_opportunity(
			lead=self._lead(
				self.item, message="PO-STOLEN", po_number="PO-STOLEN", quotation_id=quoted["quotation_id"], phone="919620204778"
			)
		)
		self.assertIn("couldn't find that quotation", result["message"])
		self.assertFalse(frappe.db.exists("Sales Order", {"po_no": "PO-STOLEN"}))

	def test_an_expired_quotation_cannot_be_confirmed(self):
		quoted = flow_api.create_whatsapp_quotation(lead=self._lead(self.item, qty="10"))
		frappe.db.set_value("Quotation", quoted["quotation_id"], "valid_till", frappe.utils.add_days(frappe.utils.today(), -1))
		result = flow_api.create_dealer_opportunity(
			lead=self._lead(self.item, message="PO-LATE", po_number="PO-LATE", quotation_id=quoted["quotation_id"])
		)
		self.assertIn("has expired", result["message"])
		self.assertFalse(frappe.db.exists("Sales Order", {"po_no": "PO-LATE"}))

	def test_a_quotation_cannot_be_confirmed_twice(self):
		quoted = flow_api.create_whatsapp_quotation(lead=self._lead(self.item, qty="10"))
		lead = self._lead(self.item, message="PO-ONCE", po_number="PO-ONCE", quotation_id=quoted["quotation_id"])
		self.assertIn("has been placed", flow_api.create_dealer_opportunity(lead=lead)["message"])
		again = flow_api.create_dealer_opportunity(lead=lead)
		self.assertIn("can't be confirmed", again["message"])
		self.assertEqual(frappe.db.count("Sales Order", {"po_no": "PO-ONCE"}), 1)
