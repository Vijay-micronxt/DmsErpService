from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.auth.dealer_api import _dealer_portal_user
from dms_erp.catalog import dealer_catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import dealer_portal_api, inquiry_api, order_api, quotation_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestDealerPortalApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Dealer Portal Test Supplier")

		cls.dealer = make_dealer("Dealer Portal Test Dealer")
		cls.dealer_user = _dealer_portal_user(cls.dealer)
		cls.other_dealer = make_dealer("Dealer Portal Other Dealer")
		cls.other_dealer_user = _dealer_portal_user(cls.other_dealer)

		cls.item = make_item("DP-TEST-ITEM", "Vitrified")
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.item, True)
		dealer_catalog_api.set_product_visibility(cls.other_dealer, cls.item, True)
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

		cls.hidden_item = make_item("DP-HIDDEN-ITEM", "Vitrified")
		dealer_catalog_api.set_product_visibility(cls.other_dealer, cls.hidden_item, True)

	def setUp(self):
		frappe.set_user(self.dealer_user)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_get_catalog_shows_every_sellable_item_but_flags_unassigned_ones(self):
		# ISS-2026-00644: hidden_item is sellable but never assigned to self.dealer --
		# it still shows up in Browse, just not orderable.
		page = dealer_portal_api.get_catalog()
		by_code = {row["code"]: row for row in page["items"]}
		self.assertIn(self.item, by_code)
		self.assertTrue(by_code[self.item]["orderable"])
		self.assertIn(self.hidden_item, by_code)
		self.assertFalse(by_code[self.hidden_item]["orderable"])

	def test_get_catalog_search_also_matches_colour_and_design_attributes(self):
		# Replaces the dealer portal's four standalone attribute dropdown filters --
		# typing the attribute value into the one search box should find it too, not
		# just an item-name match.
		frappe.db.set_value("Item", self.item, "custom_colour_attribute_1", "Rustic Grey")
		try:
			page = dealer_portal_api.get_catalog(search="Rustic Grey")
			codes = [row["code"] for row in page["items"]]
			self.assertIn(self.item, codes)
			self.assertNotIn(self.hidden_item, codes)
		finally:
			frappe.db.set_value("Item", self.item, "custom_colour_attribute_1", None)

	def test_get_catalog_search_still_matches_item_name(self):
		page = dealer_portal_api.get_catalog(search=self.item)
		codes = [row["code"] for row in page["items"]]
		self.assertIn(self.item, codes)

	def test_catalog_entry_exposes_only_this_dealers_own_code_not_the_full_list(self):
		from dms_erp.catalog.api import update_product

		frappe.set_user("Administrator")
		update_product(
			self.item,
			{
				"dealerCodes": [
					{"dealer": self.dealer, "customer_item_code": "MY-OWN-CODE", "sample_issued": 1},
					{"dealer": self.other_dealer, "customer_item_code": "THEIR-CODE", "sample_issued": 1},
				]
			},
		)
		frappe.set_user(self.dealer_user)

		entry = dealer_portal_api.get_item(self.item)
		self.assertEqual(entry["dealerCode"], "MY-OWN-CODE")
		self.assertNotIn("dealerCodes", entry)
		self.assertNotIn("THEIR-CODE", str(entry))

	def test_get_item_shows_an_unassigned_item_as_not_orderable(self):
		# ISS-2026-00644: viewing is sellability-gated, not assignment-gated --
		# `orderable: false` is what actually stops a dealer booking it.
		entry = dealer_portal_api.get_item(self.hidden_item)
		self.assertFalse(entry["orderable"])

	def test_get_item_rejects_a_nonexistent_item(self):
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_item("NO-SUCH-ITEM")

	def test_resolve_code_by_the_dealers_own_mapped_code(self):
		from dms_erp.catalog.api import update_product

		frappe.set_user("Administrator")
		update_product(self.item, {"dealerCodes": [{"dealer": self.dealer, "customer_item_code": "DLR-OWN-1", "sample_issued": 0}]})
		frappe.set_user(self.dealer_user)

		found = dealer_portal_api.resolve_code("DLR-OWN-1")
		self.assertEqual(found["code"], self.item)

	def test_resolve_code_falls_back_to_the_company_item_code(self):
		found = dealer_portal_api.resolve_code(self.item)
		self.assertEqual(found["code"], self.item)

	def test_resolve_code_finds_an_unassigned_item_but_marks_it_not_orderable(self):
		# ISS-2026-00644: resolve_code (company-code fallback) is sellability-gated,
		# not assignment-gated, same as get_catalog/get_item.
		found = dealer_portal_api.resolve_code(self.hidden_item)
		self.assertEqual(found["code"], self.hidden_item)
		self.assertFalse(found["orderable"])

	def test_resolve_code_returns_none_for_a_nonexistent_item(self):
		self.assertIsNone(dealer_portal_api.resolve_code("NO-SUCH-ITEM"))

	def test_raise_inquiry_is_web_sourced_and_unassigned(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=20, remarks="Need urgently")
		self.assertEqual(inquiry["source"], "Web")
		self.assertEqual(inquiry["dealerId"], self.dealer)
		self.assertIsNone(inquiry["assignedTo"])

	def test_raise_inquiry_allows_an_item_outside_the_catalog(self):
		# ISS-2026-00644: inquiry is the allowed action on an unassigned item --
		# convert_to_order below is what's actually blocked.
		inquiry = dealer_portal_api.raise_inquiry(item=self.hidden_item, qty=5)
		self.assertEqual(inquiry["productId"], self.hidden_item)

	def test_inquiries_carry_orderable_for_the_convert_button(self):
		# ISS-2026-00644: the portal hides "Convert to order" when this is false.
		assigned = dealer_portal_api.raise_inquiry(item=self.item, qty=5)
		unassigned = dealer_portal_api.raise_inquiry(item=self.hidden_item, qty=5)
		self.assertTrue(assigned["orderable"])
		self.assertFalse(unassigned["orderable"])
		self.assertTrue(dealer_portal_api.get_my_inquiry(assigned["id"])["orderable"])
		self.assertFalse(dealer_portal_api.get_my_inquiry(unassigned["id"])["orderable"])
		listed = {i["id"]: i["orderable"] for i in dealer_portal_api.list_my_inquiries(limit=50)["items"]}
		self.assertFalse(listed[unassigned["id"]])

	def test_convert_to_order_rejects_an_unassigned_item(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.hidden_item, qty=5)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-UNASSIGNED")

	def test_convert_to_order_requires_a_customer_po(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="")

	def test_convert_to_order_sets_po_no_and_closes_the_inquiry(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		order = dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-DEALER-001")

		self.assertEqual(order["customerPo"], "PO-DEALER-001")
		self.assertEqual(order["dealerId"], self.dealer)
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Converted to Order")
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["customerPo"], "PO-DEALER-001")

	def test_convert_to_order_rejects_an_inquiry_that_belongs_to_another_dealer(self):
		frappe.set_user(self.other_dealer_user)
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		frappe.set_user(self.dealer_user)

		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-X")

	def test_convert_to_order_rejects_an_already_converted_inquiry(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-1")

		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-2")

	@patch("dms_erp.sales.dealer_portal_api._create_order")
	def test_convert_to_order_never_leaks_erpnext_native_html_or_emails(self, mock_create_order):
		# A real report: ERPNext's own native Sales Order credit-limit check (not
		# anything this app raises itself) throws an HTML-formatted message naming
		# internal Credit Controller users and their emails -- convert_to_order
		# must never forward that verbatim to a dealer.
		mock_create_order.side_effect = frappe.ValidationError(
			"Credit limit has been crossed for customer Medha - 1(37290.0/1000.0)<br><br>"
			"Please contact any of the following users to extend the credit limits for Medha - 1: "
			"<br><br> <ul><li>Karthik Mxt (karthikeyan@micronxt.com)</li></ul>."
		)
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)

		with self.assertRaises(frappe.ValidationError) as ctx:
			dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-HTML-TEST")

		message = str(ctx.exception)
		self.assertNotIn("<", message)
		self.assertNotIn("micronxt.com", message)
		self.assertNotIn("Karthik", message)
		self.assertIn("Credit limit has been crossed for customer Medha - 1", message)
		self.assertIn("Pacific representative", message)

	def test_list_my_inquiries_never_shows_another_dealers_inquiries(self):
		dealer_portal_api.raise_inquiry(item=self.item, qty=1)
		frappe.set_user(self.other_dealer_user)
		dealer_portal_api.raise_inquiry(item=self.item, qty=2)

		frappe.set_user(self.dealer_user)
		mine = dealer_portal_api.list_my_inquiries()
		self.assertTrue(all(row["dealerId"] == self.dealer for row in mine["items"]))

	def test_list_my_orders_never_shows_another_dealers_orders(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-MINE")

		frappe.set_user(self.other_dealer_user)
		other_inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		dealer_portal_api.convert_to_order(inquiry=other_inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-THEIRS")
		mine = dealer_portal_api.list_my_orders()
		self.assertTrue(all(row["dealerId"] == self.other_dealer for row in mine["items"]))

	def test_get_my_order_rejects_another_dealers_order(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		order = dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch=add_days(today(), 7), customer_po="PO-MINE-2")

		frappe.set_user(self.other_dealer_user)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_order(order["id"])

	def test_get_my_inquiry_rejects_another_dealers_inquiry(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=1)

		frappe.set_user(self.other_dealer_user)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_inquiry(inquiry["id"])

	def test_my_dues_is_zero_with_no_invoices_raised_yet(self):
		self.assertEqual(dealer_portal_api.my_dues(), {"outstanding": 0.0})

	def test_my_profile_returns_this_dealers_own_customer_fields(self):
		profile = dealer_portal_api.my_profile()
		self.assertEqual(profile["id"], self.dealer)
		self.assertEqual(profile["name"], self.dealer)

	def test_my_profile_reports_whether_a_password_is_set(self):
		# Drives the portal's post-OTP "set an email + password" prompt.
		from frappe.utils.password import update_password

		frappe.db.sql("delete from `__Auth` where doctype='User' and fieldname='password' and name=%s", (self.dealer_user,))
		self.assertFalse(dealer_portal_api.my_profile()["hasPassword"])
		update_password(self.dealer_user, "a-long-password-1")
		self.assertTrue(dealer_portal_api.my_profile()["hasPassword"])

	def test_my_orders_never_carry_pacific_internal_fields(self):
		from frappe.utils import add_days, today

		frappe.set_user("Administrator")
		order = order_api.create_order(dealer=self.dealer, lines=[{"item": self.item, "qty": 1}], expected_dispatch=add_days(today(), 5))
		frappe.set_user(self.dealer_user)

		for o in (dealer_portal_api.get_my_order(order["id"]), *dealer_portal_api.list_my_orders()["items"]):
			for key in ("channel", "owner", "advanceConfirmed", "reservation"):
				self.assertNotIn(key, o)
			self.assertTrue(all("by" not in e for e in o["history"]))

	def test_suggest_alternatives_excludes_the_item_itself(self):
		alternatives = dealer_portal_api.suggest_alternatives(item=self.item)
		self.assertNotIn(self.item, [row["code"] for row in alternatives])

	# ---- No price before a quotation ------------------------------------------------

	def test_catalog_and_item_never_carry_any_price(self):
		# Even though this item has an approved dealer price (setUpClass): the portal
		# shows no price until a quotation.
		entry = dealer_portal_api.get_item(self.item)
		self.assertNotIn("price", entry)
		self.assertNotIn("dealerPrice", entry)
		for row in dealer_portal_api.get_catalog()["items"]:
			self.assertNotIn("price", row)
			self.assertNotIn("dealerPrice", row)

	def test_raised_and_listed_enquiries_never_carry_a_price(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=3)
		self.assertNotIn("price", inquiry)
		self.assertNotIn("price", dealer_portal_api.get_my_inquiry(inquiry["id"]))
		for row in dealer_portal_api.list_my_inquiries()["items"]:
			self.assertNotIn("price", row)

	@patch("dms_erp.sales.dealer_portal_api.all_batches")
	@patch("dms_erp.sales.dealer_portal_api.top_batches")
	def test_item_detail_lists_every_batch_but_the_catalog_list_stays_a_preview(self, mock_top, mock_all):
		batches = [{"batchNumber": f"B{i}", "itemCode": self.item, "itemName": "x", "boxes": 10 - i} for i in range(5)]
		mock_all.return_value = batches
		mock_top.return_value = batches[:3]

		self.assertEqual(len(dealer_portal_api.get_item(self.item)["topBatches"]), 5)
		self.assertEqual(len(dealer_portal_api.resolve_code(self.item)["topBatches"]), 5)
		listed = {row["code"]: row for row in dealer_portal_api.get_catalog(limit=100)["items"]}
		self.assertEqual(len(listed[self.item]["topBatches"]), 3)

	# ---- Separate section for items outside the dealer's catalogue ------------------

	def test_get_catalog_scope_splits_assigned_from_other_items(self):
		assigned = {row["code"] for row in dealer_portal_api.get_catalog(scope="assigned", limit=100)["items"]}
		other = {row["code"] for row in dealer_portal_api.get_catalog(scope="other", limit=100)["items"]}
		self.assertIn(self.item, assigned)
		self.assertNotIn(self.hidden_item, assigned)
		self.assertIn(self.hidden_item, other)
		self.assertNotIn(self.item, other)
		self.assertFalse(assigned & other)

	# ---- Several items in one enquiry -----------------------------------------------

	def test_raise_inquiries_makes_one_inquiry_per_item_sharing_a_group(self):
		result = dealer_portal_api.raise_inquiries(
			items=[{"item": self.item, "qty": 5}, {"item": self.hidden_item, "qty": 8}], remarks="For a site in Mysore"
		)
		self.assertEqual(len(result["items"]), 2)
		group = result["requestGroup"]
		self.assertEqual(group, result["items"][0]["id"])
		for row in result["items"]:
			self.assertEqual(row["requestGroup"], group)
			self.assertEqual(frappe.db.get_value("Inquiry", row["id"], "request_group"), group)
			self.assertEqual(row["source"], "Web")
			self.assertEqual(row["dealerId"], self.dealer)
		by_item = {row["productId"]: row for row in result["items"]}
		self.assertTrue(by_item[self.item]["orderable"])
		self.assertFalse(by_item[self.hidden_item]["orderable"])

	def test_raise_inquiries_accepts_a_json_string_like_a_form_post(self):
		import json

		result = dealer_portal_api.raise_inquiries(items=json.dumps([{"item": self.item, "qty": 2}]))
		self.assertEqual(len(result["items"]), 1)

	def test_raise_inquiries_validates_everything_before_creating_anything(self):
		before = frappe.db.count("Inquiry", {"dealer": self.dealer})
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.raise_inquiries(items=[{"item": self.item, "qty": 1}, {"item": "NO-SUCH-ITEM", "qty": 1}])
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.raise_inquiries(items=[{"item": self.item, "qty": 1}, {"item": self.item, "qty": 2}])
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.raise_inquiries(items=[{"item": self.item, "qty": 0}])
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.raise_inquiries(items=[])
		self.assertEqual(frappe.db.count("Inquiry", {"dealer": self.dealer}), before)

	# ---- One enquiry, however many items it holds -----------------------------------

	def _third_item(self):
		return make_item("DP-THIRD-ITEM", "Vitrified")

	def test_items_raised_together_come_back_as_one_enquiry(self):
		third = self._third_item()
		result = dealer_portal_api.raise_inquiries(
			items=[{"item": self.item, "qty": 5}, {"item": self.hidden_item, "qty": 8}, {"item": third, "qty": 3}]
		)
		group = result["requestGroup"]

		enquiries = {e["id"]: e for e in dealer_portal_api.list_my_enquiries(limit=100)["items"]}
		self.assertIn(group, enquiries)
		enquiry = enquiries[group]
		self.assertEqual(enquiry["itemCount"], 3)
		self.assertEqual({i["productId"] for i in enquiry["items"]}, {self.item, self.hidden_item, third})
		# None of the group's inquiries shows up again as an enquiry of its own.
		for row in result["items"]:
			if row["id"] != group:
				self.assertNotIn(row["id"], enquiries)
		self.assertTrue(all(i.get("productName") for i in enquiry["items"]))

	def test_a_single_item_enquiry_is_an_enquiry_of_one(self):
		single = dealer_portal_api.raise_inquiry(item=self.item, qty=4)
		enquiries = {e["id"]: e for e in dealer_portal_api.list_my_enquiries(limit=100)["items"]}
		self.assertEqual(enquiries[single["id"]]["itemCount"], 1)

	def test_enquiries_are_paged_by_enquiry_never_splitting_one(self):
		third = self._third_item()
		dealer_portal_api.raise_inquiry(item=self.item, qty=1)
		result = dealer_portal_api.raise_inquiries(
			items=[{"item": self.item, "qty": 5}, {"item": self.hidden_item, "qty": 8}, {"item": third, "qty": 3}]
		)

		first_page = dealer_portal_api.list_my_enquiries(limit=1)
		self.assertEqual(len(first_page["items"]), 1)
		# The newest enquiry is the 3-item one, whole -- not one of its three records.
		self.assertEqual(first_page["items"][0]["id"], result["requestGroup"])
		self.assertEqual(first_page["items"][0]["itemCount"], 3)
		self.assertGreaterEqual(first_page["total"], 2)

	def test_an_enquiry_status_is_the_shared_one_or_mixed(self):
		third = self._third_item()
		result = dealer_portal_api.raise_inquiries(items=[{"item": self.item, "qty": 5}, {"item": third, "qty": 3}])
		group = result["requestGroup"]
		shared = dealer_portal_api.get_my_enquiry(group)
		self.assertEqual(shared["status"], shared["items"][0]["status"])

		frappe.db.set_value("Inquiry", result["items"][1]["id"], "status", "Quoted")
		frappe.db.set_value("Inquiry", result["items"][0]["id"], "status", "Out of Stock")
		self.assertEqual(dealer_portal_api.get_my_enquiry(group)["status"], "Mixed")

	def test_get_my_enquiry_returns_a_group_or_a_single_and_never_another_dealers(self):
		result = dealer_portal_api.raise_inquiries(items=[{"item": self.item, "qty": 5}, {"item": self.hidden_item, "qty": 8}])
		group = result["requestGroup"]
		self.assertEqual(dealer_portal_api.get_my_enquiry(group)["itemCount"], 2)
		single = dealer_portal_api.raise_inquiry(item=self.item, qty=2)
		self.assertEqual(dealer_portal_api.get_my_enquiry(single["id"])["itemCount"], 1)

		frappe.set_user(self.other_dealer_user)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_enquiry(group)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_enquiry(single["id"])

	# ---- Quotations the dealer receives and accepts ---------------------------------

	def _staff_quotation(self, dealer=None, inquiries=None, validity_days=7, freight=1200):
		frappe.set_user("Administrator")
		try:
			return quotation_api.create_quotation(
				dealer=dealer or self.dealer,
				lines=[{"item": self.item, "qty": 10}],
				markup_pct=12,
				freight=freight,
				validity_days=validity_days,
				inquiries=inquiries,
			)
		finally:
			frappe.set_user(self.dealer_user)

	def test_dealer_sees_their_quotation_without_any_internal_numbers(self):
		quotation = self._staff_quotation()

		listed = {row["id"]: row for row in dealer_portal_api.list_my_quotations()["items"]}
		self.assertIn(quotation["id"], listed)
		detail = dealer_portal_api.get_my_quotation(quotation["id"])
		self.assertTrue(detail["canAccept"])
		self.assertEqual(detail["lines"][0]["itemCode"], self.item)
		self.assertEqual(detail["lines"][0]["rate"], 403)  # round(360 * 1.12)
		self.assertEqual(detail["freight"], 1200)
		self.assertEqual(detail["totalPayable"], detail["netTotal"] + detail["totalTaxesAndCharges"] + 1200)
		# Pacific's own markup %, sales channel and the item's purchase cost are staff-only.
		for key in ("markupPct", "channel"):
			self.assertNotIn(key, detail)
		for line in detail["lines"]:
			self.assertNotIn("purchaseCost", line)
			self.assertNotIn("belowPurchaseCost", line)

	def test_a_dealer_never_sees_another_dealers_quotation(self):
		quotation = self._staff_quotation(dealer=self.other_dealer)
		self.assertNotIn(quotation["id"], [row["id"] for row in dealer_portal_api.list_my_quotations()["items"]])
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_quotation(quotation["id"])
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.accept_quotation(quotation["id"], add_days(today(), 10), "PO-STOLEN")

	def test_accepting_a_quotation_places_the_order_as_quoted_with_the_dealers_po(self):
		first = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		second = dealer_portal_api.raise_inquiry(item=self.item, qty=4)
		quotation = self._staff_quotation(inquiries=[first["id"], second["id"]])

		order = dealer_portal_api.accept_quotation(quotation["id"], add_days(today(), 10), "  PO-ACCEPT-1 ")

		self.assertEqual(order["customerPo"], "PO-ACCEPT-1")
		self.assertEqual(order["sourceType"], "Quotation")
		self.assertEqual(order["sourceRef"], quotation["id"])
		self.assertEqual(order["lines"][0]["rate"], 403)
		self.assertEqual(order["lines"][0]["qty"], 10)
		# Every inquiry the quotation covered closes, not only the first one.
		for inquiry in (first, second):
			self.assertEqual(frappe.db.get_value("Inquiry", inquiry["id"], "status"), "Converted to Order")
			self.assertEqual(frappe.db.get_value("Inquiry", inquiry["id"], "linked_sales_order"), order["id"])
		self.assertFalse(dealer_portal_api.get_my_quotation(quotation["id"])["canAccept"])

	def test_a_quotation_cannot_be_accepted_twice(self):
		quotation = self._staff_quotation()
		dealer_portal_api.accept_quotation(quotation["id"], add_days(today(), 10), "PO-ONCE")
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.accept_quotation(quotation["id"], add_days(today(), 10), "PO-TWICE")

	def test_accepting_needs_a_po_number(self):
		quotation = self._staff_quotation()
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.accept_quotation(quotation["id"], add_days(today(), 10), "   ")

	def test_an_expired_quotation_cannot_be_accepted(self):
		quotation = self._staff_quotation()
		frappe.db.set_value("Quotation", quotation["id"], "valid_till", add_days(today(), -1))
		detail = dealer_portal_api.get_my_quotation(quotation["id"])
		self.assertFalse(detail["canAccept"])
		self.assertEqual(detail["status"], "Expired")
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.accept_quotation(quotation["id"], add_days(today(), 10), "PO-LATE")

	# ---- Invoices on the portal -----------------------------------------------------

	def _invoice_for(self, dealer):
		# Undone after the test: an invoice left in the shared class transaction would
		# otherwise leak into test_my_dues_is_zero_with_no_invoices_raised_yet.
		savepoint = f"dp_invoice_{dealer.replace(' ', '_')}"
		frappe.db.savepoint(savepoint)
		self.addCleanup(frappe.db.rollback, save_point=savepoint)
		frappe.set_user("Administrator")
		try:
			order = order_api.create_order(
				dealer=dealer, lines=[{"item": self.item, "qty": 2}], expected_dispatch=add_days(today(), 5)
			)
			frappe.db.set_value("Sales Order", order["id"], "custom_fulfillment_stage", "Dispatched")
			invoice = order_api.create_sales_invoice(order["id"])
			return invoice["invoices"][0]["id"] if "invoices" in invoice else invoice["id"]
		finally:
			frappe.set_user(self.dealer_user)

	def test_dealer_sees_only_their_own_invoices(self):
		mine = self._invoice_for(self.dealer)
		theirs = self._invoice_for(self.other_dealer)

		listed = [row["id"] for row in dealer_portal_api.list_my_invoices(limit=100)["items"]]
		self.assertIn(mine, listed)
		self.assertNotIn(theirs, listed)
		detail = dealer_portal_api.get_my_invoice(mine)
		self.assertEqual(detail["id"], mine)
		self.assertEqual(detail["items"][0]["itemCode"], self.item)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_invoice(theirs)

	def test_invoice_search_finds_by_number(self):
		mine = self._invoice_for(self.dealer)
		found = [row["id"] for row in dealer_portal_api.list_my_invoices(search=mine)["items"]]
		self.assertEqual(found, [mine])
		self.assertEqual(dealer_portal_api.list_my_invoices(search="NO-SUCH-INVOICE")["items"], [])

	# ---- The alternative item staff set --------------------------------------------

	def _with_alternative(self, alt_code):
		from dms_erp.catalog.api import _set_alt_item

		out_of_stock = make_item("DP-ALT-SOURCE", "Vitrified")
		_set_alt_item(out_of_stock, alt_code)
		return out_of_stock

	@patch("dms_erp.sales.dealer_portal_api.total_stock_for_item")
	def test_the_alternative_staff_chose_comes_first_and_is_flagged(self, mock_stock):
		mock_stock.side_effect = lambda code: 10 if code == self.item else 0
		source = self._with_alternative(self.item)

		result = dealer_portal_api.suggest_alternatives(item=source)

		self.assertEqual(result[0]["id"], self.item)
		self.assertTrue(result[0]["isRecommended"])
		self.assertEqual([r["id"] for r in result].count(self.item), 1)  # never listed twice
		for other in result[1:]:
			self.assertFalse(other["isRecommended"])

	@patch("dms_erp.sales.dealer_portal_api.total_stock_for_item")
	def test_an_alternative_with_no_stock_is_not_recommended(self, mock_stock):
		mock_stock.return_value = 0
		source = self._with_alternative(self.item)

		result = dealer_portal_api.suggest_alternatives(item=source)

		self.assertFalse(any(r["isRecommended"] for r in result))

	@patch("dms_erp.sales.dealer_portal_api.total_stock_for_item")
	def test_an_alternative_outside_the_dealers_catalog_is_not_recommended(self, mock_stock):
		mock_stock.return_value = 10
		source = self._with_alternative(self.hidden_item)  # assigned to the other dealer only

		result = dealer_portal_api.suggest_alternatives(item=source)

		self.assertNotIn(self.hidden_item, [r["id"] for r in result])
		self.assertFalse(any(r["isRecommended"] for r in result))

	def test_a_staff_only_session_cannot_use_the_dealer_portal(self):
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_catalog()


class TestUpdateMyEmail(FrappeTestCase):
	"""A dedicated dealer+user, separate from TestDealerPortalApi's shared fixtures
	above, since this mutates Customer.custom_email -- now a login-password lookup
	key (auth.dealer_api.login_with_password), so it shouldn't bleed into any of
	those other tests."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Update My Email Test Dealer")
		cls.dealer_user = _dealer_portal_user(cls.dealer)
		cls.other_dealer = make_dealer("Update My Email Other Test Dealer")
		frappe.db.set_value("Customer", cls.other_dealer, "custom_email", "taken@pacific.example")

	def setUp(self):
		frappe.set_user(self.dealer_user)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("Customer", self.dealer, "custom_email", None)

	def test_update_my_email_sets_it_and_returns_the_updated_profile(self):
		profile = dealer_portal_api.update_my_email(email="dealer@pacific.example")
		self.assertEqual(profile["email"], "dealer@pacific.example")
		self.assertEqual(dealer_portal_api.my_profile()["email"], "dealer@pacific.example")

	def test_update_my_email_rejects_an_email_already_used_by_another_dealer(self):
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.update_my_email(email="taken@pacific.example")

	def test_update_my_email_requires_a_value(self):
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.update_my_email(email="")
