import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog import dealer_catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, quotation_api
from dms_erp.sales.dealer_address_api import create_dealer_address
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_dealer_addresses,
	make_item,
	make_supplier,
)


# Relative to today: fixed dates rotted into the past, and ERPNext then refuses the order
# ("Expected Delivery Date should be after Sales Order Date"). LINE_DELIVERY is a line's
# own date, deliberately later than DISPATCH so the two can't be confused.
DISPATCH = str(add_days(today(), 30))
LINE_DELIVERY = str(add_days(today(), 40))


class TestQuotationApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Quotation Test Supplier")
		cls.dealer = make_dealer("Quotation Test Dealer")
		cls.billing_address, cls.shipping_address = make_dealer_addresses(cls.dealer)

		cls.priced_item = make_item("QTN-PRICED", "Vitrified")
		pricing_api.ensure_price_record(cls.priced_item, cls.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=cls.priced_item, final_price=500, reason="Launch")

		cls.unpriced_item = make_item("QTN-UNPRICED", "Vitrified")

		cls.second_item = make_item("QTN-SECOND", "Vitrified")
		pricing_api.ensure_price_record(cls.second_item, cls.supplier, 200, 25, "2026-08-01")
		pricing_api.approve_price(item=cls.second_item, final_price=250, reason="Launch")

		dealer_catalog_api.set_product_visibility(cls.dealer, cls.priced_item, True)
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.unpriced_item, True)
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.second_item, True)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _create_quotation(self, dealer=None, **kwargs):
		"""BRD C.3.3 made customer_address/shipping_address_name mandatory on
		create_quotation -- every test below that isn't specifically exercising
		that validation goes through here instead of calling create_quotation
		directly, so a real Billing + Shipping Address is always on file for
		whichever dealer is quoting."""
		dealer = dealer or self.dealer
		billing, shipping = make_dealer_addresses(dealer)
		kwargs.setdefault("customer_address", billing)
		kwargs.setdefault("shipping_address_name", shipping)
		return quotation_api.create_quotation(dealer=dealer, **kwargs)

	def test_create_quotation_exposes_created_at_timestamp(self):
		# Quotation.transaction_date is a plain Date field (no time-of-day) --
		# createdAt is the actual creation Datetime, for tables that need to show
		# time alongside date.
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12
		)
		self.assertIsNotNone(quotation["createdAt"])

	def test_create_quotation_applies_markup_to_approved_price(self):
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 100}], markup_pct=12
		)
		self.assertEqual(quotation["lines"][0]["rate"], 560)  # round(500 * 1.12)
		self.assertEqual(quotation["channel"], "Retail")  # default

	def test_create_quotation_exposes_the_addresses_it_was_created_with(self):
		# BRD C.3.3 -- a staff-facing client (e.g. the Quotation Builder) needs to be able to
		# read back which addresses were actually picked, not just that creation succeeded.
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12
		)
		self.assertEqual(quotation["customerAddress"], self.billing_address)
		self.assertEqual(quotation["shippingAddressName"], self.shipping_address)

	def test_create_quotation_stores_delivery_pincode(self):
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12, delivery_pincode="560001"
		)
		self.assertEqual(quotation["deliveryPincode"], "560001")

	def test_suggest_transport_rate_returns_none_for_unknown_pincode(self):
		self.assertIsNone(quotation_api.suggest_transport_rate("999999"))

	def _make_zone(self, zone_name, from_pincode, to_pincode, rate_basis="Per Trip", rate=4500, **extra):
		doc = frappe.get_doc(
			{
				"doctype": "Transport Rate Zone",
				"zone_name": zone_name,
				"from_pincode": from_pincode,
				"to_pincode": to_pincode,
				"rate_basis": rate_basis,
				"rate": rate,
				**extra,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Transport Rate Zone", doc.name, force=True, ignore_permissions=True)
		return doc

	def test_suggest_transport_rate_matches_a_pincode_inside_a_range(self):
		self._make_zone("QT Bengaluru Central", "560001", "560050")
		suggestion = quotation_api.suggest_transport_rate("560023")
		self.assertEqual(len(suggestion["options"]), 1)
		self.assertEqual(suggestion["options"][0]["zone"], "QT Bengaluru Central")
		self.assertEqual(suggestion["options"][0]["suggestedFreight"], 4500)
		self.assertIsNone(quotation_api.suggest_transport_rate("560051"))

	def test_suggest_transport_rate_prices_per_kg_and_per_km(self):
		self._make_zone("QT Per Kg", "561001", "561010", rate_basis="Per Kg", rate=2, vehicle_type="Tata 407")
		self._make_zone("QT Per Km", "561001", "561010", rate_basis="Per Km", rate=40, distance_km=120, vehicle_type="Lorry")
		by_zone = {o["zone"]: o for o in quotation_api.suggest_transport_rate("561005", weight_kg=1500)["options"]}
		self.assertEqual(by_zone["QT Per Kg"]["suggestedFreight"], 3000)
		self.assertEqual(by_zone["QT Per Km"]["suggestedFreight"], 4800)
		# No weight -> a Per Kg option can't be priced, so no number rather than a guess.
		by_zone = {o["zone"]: o for o in quotation_api.suggest_transport_rate("561005")["options"]}
		self.assertIsNone(by_zone["QT Per Kg"]["suggestedFreight"])

	def test_transport_rate_zone_rejects_overlap_for_the_same_vehicle_type(self):
		self._make_zone("QT Overlap A", "562001", "562050", vehicle_type="Lorry")
		with self.assertRaises(frappe.ValidationError):
			self._make_zone("QT Overlap B", "562040", "562060", vehicle_type="Lorry")
		# A different vehicle type over the same range is the intended case.
		self._make_zone("QT Overlap C", "562040", "562060", vehicle_type="Tata 407")

	def test_quotation_line_flags_a_rate_below_purchase_cost(self):
		# priced_item's Item Price Proposal records a purchase cost of 400.
		below = quotation_api._serialize_line(frappe._dict(item_code=self.priced_item, qty=1, rate=350))
		self.assertEqual(below["purchaseCost"], 400)
		self.assertTrue(below["belowPurchaseCost"])
		above = quotation_api._serialize_line(frappe._dict(item_code=self.priced_item, qty=1, rate=560))
		self.assertFalse(above["belowPurchaseCost"])
		# No recorded cost -> no flag, never a comparison against a made-up number.
		unknown = quotation_api._serialize_line(frappe._dict(item_code=self.unpriced_item, qty=1, rate=1))
		self.assertIsNone(unknown["purchaseCost"])
		self.assertFalse(unknown["belowPurchaseCost"])

	def test_quotation_line_carries_the_items_weight(self):
		frappe.db.set_value("Item", self.priced_item, "custom_weight_per_box_kg", 28)
		quotation = self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12)
		self.assertEqual(quotation["lines"][0]["weightPerBoxKg"], 28)
		self.assertEqual(quotation["lines"][0]["totalWeightKg"], 280)

	def test_quotation_line_carries_the_items_pieces_and_sqft(self):
		frappe.db.set_value("Item", self.priced_item, "custom_pieces_per_box", 4)
		frappe.db.set_value("Item", self.priced_item, "custom_sqft_per_box", 15.5)
		quotation = self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12)
		self.assertEqual(quotation["lines"][0]["piecesPerBox"], 4)
		self.assertEqual(quotation["lines"][0]["totalPieces"], 40)
		self.assertEqual(quotation["lines"][0]["sqftPerBox"], 15.5)
		self.assertEqual(quotation["lines"][0]["totalSqft"], 155)
		self.assertAlmostEqual(quotation["lines"][0]["sqmPerBox"], 1.44, places=2)
		self.assertAlmostEqual(quotation["lines"][0]["totalSqm"], 14.4, places=2)

	def test_create_quotation_accepts_bulk_channel_and_carries_into_order(self):
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 500}], markup_pct=8, channel="Bulk"
		)
		self.assertEqual(quotation["channel"], "Bulk")

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch=DISPATCH)
		self.assertEqual(order["channel"], "Bulk")

	def test_create_quotation_rejects_invalid_channel(self):
		with self.assertRaises(frappe.ValidationError):
			self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10, channel="Wholesale")

	def test_create_quotation_auto_classifies_bulk_from_the_dealers_type(self):
		bulk_dealer = make_dealer("Quotation Bulk-Type Dealer")
		dealer_catalog_api.set_product_visibility(bulk_dealer, self.priced_item, True)
		frappe.db.set_value("Customer", bulk_dealer, "custom_dealer_type", "Bulk")

		quotation = self._create_quotation(dealer=bulk_dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)
		self.assertEqual(quotation["channel"], "Bulk")

	def test_create_quotation_auto_classifies_bulk_from_the_items_series_threshold(self):
		frappe.db.set_value("Item", self.priced_item, "custom_bulk_qty_threshold", 50)
		try:
			quotation = self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 100}], markup_pct=10)
			self.assertEqual(quotation["channel"], "Bulk")
		finally:
			frappe.db.set_value("Item", self.priced_item, "custom_bulk_qty_threshold", 0)

	def test_create_quotation_rejects_item_outside_dealer_catalog(self):
		# A dealer with no Dealer Catalog at all may quote anything (is_visible's
		# unassigned fallback) -- give this one a catalog that just doesn't include the item.
		other_dealer = make_dealer("Quotation Test Dealer 2")
		dealer_catalog_api.set_product_visibility(other_dealer, self.second_item, True)
		dealer_catalog_api.set_product_visibility(other_dealer, self.priced_item, False)
		with self.assertRaises(frappe.PermissionError):
			self._create_quotation(dealer=other_dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)

	def test_create_quotation_rejects_unpriced_item(self):
		with self.assertRaises(frappe.ValidationError):
			self._create_quotation(dealer=self.dealer, lines=[{"item": self.unpriced_item, "qty": 10}], markup_pct=10)

	def test_create_quotation_rejects_pulled_back_item_even_when_still_visible(self):
		frappe.db.set_value("Item", self.priced_item, "custom_discontinuation_status", "Pulled Back")
		try:
			# Still assigned/visible — set_product_visibility isn't retroactively cleaned up.
			self.assertTrue(dealer_catalog_api.is_visible(self.dealer, self.priced_item))
			with self.assertRaises(frappe.ValidationError):
				self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)
		finally:
			frappe.db.set_value("Item", self.priced_item, "custom_discontinuation_status", "Active")

	def test_create_quotation_rejection_uses_the_configured_white_label_wording(self):
		settings = frappe.get_single("DMS Catalog Settings")
		settings.append("status_labels", {"status": "Pulled Back", "label": "PDC"})
		settings.save(ignore_permissions=True)
		frappe.db.set_value("Item", self.priced_item, "custom_discontinuation_status", "Pulled Back")
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)
			self.assertIn("PDC", str(ctx.exception))
		finally:
			frappe.db.set_value("Item", self.priced_item, "custom_discontinuation_status", "Active")
			settings = frappe.get_single("DMS Catalog Settings")
			settings.status_labels = []
			settings.save(ignore_permissions=True)

	def test_create_quotation_from_inquiry_marks_it_quoted(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=50, source="Phone")
		created = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 50}], markup_pct=10, inquiries=[inquiry["id"]]
		)
		updated = inquiry_api.get_inquiry(inquiry["id"])
		self.assertEqual(updated["status"], "Quoted")
		self.assertEqual(updated["linkedQuotation"], created["id"])

	def test_create_quotation_merges_two_inquiries_from_the_same_dealer(self):
		i1 = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=20, source="Phone")
		i2 = inquiry_api.create_inquiry(dealer=self.dealer, item=self.second_item, qty=15, source="Phone")
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 20}, {"item": self.second_item, "qty": 15}],
			markup_pct=10,
			inquiries=[i1["id"], i2["id"]],
		)
		self.assertEqual(len(quotation["lines"]), 2)
		self.assertEqual(inquiry_api.get_inquiry(i1["id"])["status"], "Quoted")
		self.assertEqual(inquiry_api.get_inquiry(i2["id"])["status"], "Quoted")
		self.assertEqual(inquiry_api.get_inquiry(i1["id"])["linkedQuotation"], quotation["id"])
		self.assertEqual(inquiry_api.get_inquiry(i2["id"])["linkedQuotation"], quotation["id"])

	def test_create_quotation_rejects_inquiries_from_a_different_dealer(self):
		other_dealer = make_dealer("Quotation Test Other Dealer")
		i1 = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=20, source="Phone")
		i2 = inquiry_api.create_inquiry(dealer=other_dealer, item=self.second_item, qty=15, source="Phone")
		with self.assertRaises(frappe.ValidationError):
			self._create_quotation(
				dealer=self.dealer,
				lines=[{"item": self.priced_item, "qty": 20}, {"item": self.second_item, "qty": 15}],
				markup_pct=10,
				inquiries=[i1["id"], i2["id"]],
			)

	def test_add_quotation_line_amends_and_reprices_every_line(self):
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 100}], markup_pct=12
		)

		amended = quotation_api.add_quotation_line(quotation["id"], self.second_item, 20)

		self.assertNotEqual(amended["id"], quotation["id"])
		self.assertEqual(len(amended["lines"]), 2)
		by_item = {l["itemCode"]: l for l in amended["lines"]}
		self.assertEqual(by_item[self.priced_item]["rate"], 560)  # round(500 * 1.12), rebuilt not stale
		self.assertEqual(by_item[self.second_item]["rate"], 280)  # round(250 * 1.12)
		self.assertEqual(frappe.db.get_value("Quotation", quotation["id"], "docstatus"), 2)

	def test_remove_quotation_line_drops_it_and_rejects_last_line(self):
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 100}, {"item": self.second_item, "qty": 20}],
			markup_pct=10,
		)

		amended = quotation_api.remove_quotation_line(quotation["id"], self.second_item)
		self.assertEqual(len(amended["lines"]), 1)
		self.assertEqual(amended["lines"][0]["itemCode"], self.priced_item)

		with self.assertRaises(frappe.ValidationError):
			quotation_api.remove_quotation_line(amended["id"], self.priced_item)

	def test_update_quotation_line_qty_changes_only_that_line(self):
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 100}, {"item": self.second_item, "qty": 20}],
			markup_pct=10,
		)

		amended = quotation_api.update_quotation_line_qty(quotation["id"], self.second_item, 50)
		by_item = {l["itemCode"]: l for l in amended["lines"]}
		self.assertEqual(by_item[self.second_item]["qty"], 50)
		self.assertEqual(by_item[self.priced_item]["qty"], 100)

	def test_update_quotation_line_qty_does_not_crash_when_a_line_has_a_delivery_date(self):
		# Regression test: update_quotation_line_qty reloads the Quotation from the
		# database before touching any line (frappe.get_doc, not the in-memory doc
		# create_quotation just returned) -- a freshly-loaded Quotation Item row only
		# carries attributes for its own real fields, so reading a field that was
		# never actually a Quotation Item field (delivery_date is native on Sales
		# Order Item, not Quotation Item) raised a genuine AttributeError here in
		# production the moment a dealer tried to change a line's quantity, even
		# though the quotation with that delivery date had been created successfully
		# moments earlier (see sales/setup.py's own docstring for the full story).
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 100, "delivery_date": LINE_DELIVERY}],
			markup_pct=10,
		)

		amended = quotation_api.update_quotation_line_qty(quotation["id"], self.priced_item, 150)

		line = amended["lines"][0]
		self.assertEqual(line["qty"], 150)
		self.assertEqual(str(line["deliveryDate"]), LINE_DELIVERY)

	def test_edit_rejects_already_ordered_quotation(self):
		quotation = self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)
		quotation_api.convert_to_order(quotation["id"], expected_dispatch=DISPATCH)

		with self.assertRaises(frappe.ValidationError):
			quotation_api.update_quotation_line_qty(quotation["id"], self.priced_item, 5)

	def test_update_quotation_status_marks_lost(self):
		quotation = self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)

		updated = quotation_api.update_quotation_status(quotation["id"], "Lost", detailed_reason="Dealer chose a competitor")
		self.assertEqual(updated["status"], "Lost")

	def test_update_quotation_status_rejects_other_statuses(self):
		quotation = self._create_quotation(dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10)

		with self.assertRaises(frappe.ValidationError):
			quotation_api.update_quotation_status(quotation["id"], "Open")

	def test_convert_to_order_creates_sales_order_and_closes_inquiry(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.priced_item, qty=30, source="Phone")
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 30}], markup_pct=15, inquiries=[inquiry["id"]]
		)

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch=DISPATCH)

		self.assertEqual(order["sourceType"], "Quotation")
		self.assertEqual(order["sourceRef"], quotation["id"])
		self.assertEqual(order["stage"], "Confirmed")
		self.assertEqual(len(order["history"]), 2)
		self.assertEqual(order["history"][0]["stage"], "Created")
		self.assertEqual(order["history"][1]["stage"], "Confirmed")
		converted = inquiry_api.get_inquiry(inquiry["id"])
		self.assertEqual(converted["status"], "Converted to Order")
		self.assertEqual(converted["linkedSalesOrder"], order["id"])

	def test_list_quotations_is_paginated_and_searchable(self):
		dealer = make_dealer("Quotation Pagination Dealer")
		dealer_catalog_api.set_product_visibility(dealer, self.priced_item, True)
		created = [
			self._create_quotation(dealer=dealer, lines=[{"item": self.priced_item, "qty": 5}], markup_pct=10)
			for _ in range(3)
		]

		page = quotation_api.list_quotations(dealer=dealer, limit=2, offset=0)
		self.assertEqual(page["total"], 3)
		self.assertEqual(len(page["items"]), 2)

		next_page = quotation_api.list_quotations(dealer=dealer, limit=2, offset=2)
		self.assertEqual(len(next_page["items"]), 1)

		found = quotation_api.list_quotations(dealer=dealer, search=created[0]["id"])
		self.assertEqual(found["total"], 1)
		self.assertEqual(found["items"][0]["id"], created[0]["id"])

	def test_create_quotation_applies_line_level_discount(self):
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 10}],
			markup_pct=12,
		)
		line = quotation["lines"][0]
		self.assertEqual(line["priceListRate"], 560)  # round(500 * 1.12)
		self.assertEqual(line["discountPercentage"], 10)
		self.assertEqual(line["rate"], 504)  # round(560 * 0.9)

	def test_create_quotation_rejects_an_out_of_range_discount(self):
		with self.assertRaises(frappe.ValidationError):
			self._create_quotation(
				dealer=self.dealer,
				lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": -5}],
				markup_pct=12,
			)

	def test_create_quotation_respects_a_per_line_delivery_date(self):
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "delivery_date": LINE_DELIVERY}],
			markup_pct=12,
		)
		self.assertEqual(str(quotation["lines"][0]["deliveryDate"]), LINE_DELIVERY)

	def test_add_quotation_line_preserves_discount_and_delivery_date_on_untouched_lines(self):
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 100, "discount_percentage": 10, "delivery_date": LINE_DELIVERY}],
			markup_pct=12,
		)

		amended = quotation_api.add_quotation_line(quotation["id"], self.second_item, 20)

		by_item = {l["itemCode"]: l for l in amended["lines"]}
		# The untouched line's own discount/date must survive the reprice, not
		# silently reset to 0/None just because a different line was added.
		self.assertEqual(by_item[self.priced_item]["discountPercentage"], 10)
		self.assertEqual(str(by_item[self.priced_item]["deliveryDate"]), LINE_DELIVERY)
		self.assertEqual(by_item[self.priced_item]["rate"], 504)  # round(560 * 0.9), rebuilt not stale

	def test_convert_to_order_carries_discount_and_delivery_date_via_native_mapper(self):
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10, "discount_percentage": 10, "delivery_date": LINE_DELIVERY}],
			markup_pct=12,
		)

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch=DISPATCH)

		line = order["lines"][0]
		self.assertEqual(line["discountPercentage"], 10)
		self.assertEqual(line["rate"], 504)
		# The quotation line's own date wins over the order-level expected_dispatch.
		self.assertEqual(str(line["deliveryDate"]), LINE_DELIVERY)

	def test_convert_to_order_only_fills_the_gap_for_lines_with_no_own_delivery_date(self):
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12
		)

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch=DISPATCH)

		self.assertEqual(order["lines"][0]["deliveryDate"], DISPATCH)

	def test_convert_to_order_carries_damaged_stock_flag_via_native_mapper(self):
		"""ISS-2026-00678 -- custom_damaged_stock is the same fieldname on both
		Quotation Item and Sales Order Item, so ERPNext's native mapper carries it
		across with no extra glue code (unlike custom_delivery_date above, which
		needs one because the two doctypes name it differently)."""
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[
				{"item": self.priced_item, "qty": 10, "damaged_stock": True},
				{"item": self.second_item, "qty": 5},
			],
			markup_pct=10,
		)
		self.assertTrue(quotation["lines"][0]["damagedStock"])
		self.assertFalse(quotation["lines"][1]["damagedStock"])

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch=DISPATCH)

		by_item = {line["itemCode"]: line for line in order["lines"]}
		self.assertTrue(by_item[self.priced_item]["damagedStock"])
		self.assertFalse(by_item[self.second_item]["damagedStock"])

	def _make_tax_template(self, name_suffix: str, rate: float = 18, is_default: bool = False) -> str:
		company = ensure_company()
		account = frappe.get_all("Account", filters={"company": company, "is_group": 0}, limit=1, pluck="name")
		if not account:
			self.skipTest("Test company has no Chart of Accounts to pick a leaf account from.")
		template_name = f"Quotation Test GST {name_suffix}"
		if frappe.db.exists("Sales Taxes and Charges Template", {"title": template_name, "company": company}):
			existing = frappe.db.get_value("Sales Taxes and Charges Template", {"title": template_name, "company": company}, "name")
			if is_default:
				existing_doc = frappe.get_doc("Sales Taxes and Charges Template", existing)
				existing_doc.is_default = 1
				existing_doc.save(ignore_permissions=True)
			return existing
		template = frappe.get_doc(
			{
				"doctype": "Sales Taxes and Charges Template",
				"title": template_name,
				"company": company,
				"is_default": 1 if is_default else 0,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": account[0],
						"description": template_name,
						"rate": rate,
					}
				],
			}
		)
		template.insert(ignore_permissions=True)
		return template.name

	def test_create_quotation_applies_a_tax_template_and_lets_erpnext_compute_the_total(self):
		template_name = self._make_tax_template("A", rate=18)
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=0, taxes_and_charges=template_name
		)
		self.assertEqual(quotation["taxesAndCharges"], template_name)
		self.assertEqual(len(quotation["taxes"]), 1)
		self.assertEqual(quotation["netTotal"], 5000)  # 10 * 500
		self.assertEqual(quotation["totalTaxesAndCharges"], 900)  # 18% of 5000
		self.assertEqual(quotation["total"], 5900)

	def test_create_quotation_without_a_tax_template_stays_untaxed(self):
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10
		)
		self.assertFalse(quotation["taxesAndCharges"])
		self.assertEqual(quotation["taxes"], [])

	def test_create_quotation_without_a_tax_template_ignores_the_companys_default_template(self):
		"""Regression for the "hardcoded 18% for all customers" QA report: on a site
		with a default Sales Taxes and Charges Template configured, ERPNext's own
		validate()-time logic (Accounts Settings > "Add taxes from Taxes and Charges/
		Item Tax Template") would otherwise silently apply it to any new quotation
		whose taxes table is still empty -- regardless of what the caller asked for."""
		self._make_tax_template("Default", rate=18, is_default=True)
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10
		)
		self.assertFalse(quotation["taxesAndCharges"])
		self.assertEqual(quotation["taxes"], [])
		self.assertEqual(quotation["totalTaxesAndCharges"], 0)
		self.assertEqual(quotation["total"], quotation["netTotal"])

	def test_convert_to_order_of_an_untaxed_quotation_ignores_the_companys_default_template(self):
		"""Same regression, for the Quotation -> Sales Order conversion path: ERPNext's
		make_sales_order mapper carries the source quotation's (empty) taxes across
		verbatim, and the resulting Sales Order's own insert() is just as exposed to
		the company's default-template auto-population as a directly-created order."""
		self._make_tax_template("Default Convert", rate=18, is_default=True)
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=10
		)
		self.assertFalse(quotation["taxesAndCharges"])

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch=DISPATCH)
		self.assertFalse(order["taxesAndCharges"])
		self.assertEqual(order["taxes"], [])
		self.assertEqual(order["total"], order["netTotal"])

	def _make_uom_item(self):
		item = make_item("QTN-UOM-TEST-ITEM", "Vitrified")
		frappe.db.set_value(
			"Item",
			item,
			{"custom_pieces_per_box": 4, "custom_sqft_per_box": 15.5, "custom_weight_per_box_kg": 28},
		)
		pricing_api.ensure_price_record(item, self.supplier, 400, 25, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=500, reason="Launch")
		dealer_catalog_api.set_product_visibility(self.dealer, item, True)
		return item

	def test_quotation_line_entered_in_sqft_is_converted_to_boxes(self):
		item = self._make_uom_item()
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": item, "qty": 50, "uom": "Sqft"}], markup_pct=12
		)
		line = quotation["lines"][0]
		self.assertEqual(line["qty"], 4)  # ceil(50 / 15.5) = 4 boxes
		self.assertEqual(line["enteredQty"], 50)
		self.assertEqual(line["enteredUom"], "Sqft")
		self.assertEqual(line["amount"], 4 * 560)  # round(500 * 1.12) = 560/box

	def test_quotation_line_left_in_box_carries_no_entered_fields(self):
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10}], markup_pct=12
		)
		line = quotation["lines"][0]
		self.assertIsNone(line["enteredQty"])
		self.assertIsNone(line["enteredUom"])

	def test_quotation_line_uom_without_a_conversion_factor_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._create_quotation(
				dealer=self.dealer, lines=[{"item": self.priced_item, "qty": 10, "uom": "Pcs"}], markup_pct=12
			)

	def test_update_quotation_line_qty_clears_stale_entered_uom(self):
		"""A direct box-qty override on an amend must drop the line's own entered-unit
		annotation -- it would otherwise go stale against the new qty."""
		item = self._make_uom_item()
		quotation = self._create_quotation(
			dealer=self.dealer, lines=[{"item": item, "qty": 50, "uom": "Sqft"}], markup_pct=12
		)
		updated = quotation_api.update_quotation_line_qty(quotation["id"], item, 9)
		line = updated["lines"][0]
		self.assertEqual(line["qty"], 9)
		self.assertIsNone(line["enteredQty"])
		self.assertIsNone(line["enteredUom"])

	# -- BRD C.3.3: bill-to/ship-to mandatory addresses --

	def test_create_quotation_rejects_a_missing_address(self):
		with self.assertRaises(frappe.ValidationError):
			quotation_api.create_quotation(
				dealer=self.dealer,
				lines=[{"item": self.priced_item, "qty": 10}],
				markup_pct=10,
				customer_address="",
				shipping_address_name=self.shipping_address,
			)

	def test_create_quotation_rejects_an_address_not_on_file_for_the_dealer(self):
		other_dealer = make_dealer("Quotation Foreign Address Dealer")
		other_billing, _ = make_dealer_addresses(other_dealer)
		with self.assertRaises(frappe.ValidationError):
			quotation_api.create_quotation(
				dealer=self.dealer,
				lines=[{"item": self.priced_item, "qty": 10}],
				markup_pct=10,
				customer_address=other_billing,
				shipping_address_name=self.shipping_address,
			)

	def test_convert_to_order_carries_the_quotations_addresses_with_no_extra_glue(self):
		# customer_address/shipping_address_name are identically-named native fields
		# on both Quotation and Sales Order, so ERPNext's own make_sales_order field
		# mapper must carry them forward with no glue code in _convert_to_order.
		other_shipping = create_dealer_address(
			dealer=self.dealer,
			address_type="Shipping",
			address_line1="Quotation Test Alt Shipping St",
			city="Test City",
			state="Gujarat",
			pincode="360003",
		)["id"]
		quotation = self._create_quotation(
			dealer=self.dealer,
			lines=[{"item": self.priced_item, "qty": 10}],
			markup_pct=10,
			customer_address=self.billing_address,
			shipping_address_name=other_shipping,
		)
		self.assertEqual(quotation["lines"][0]["itemCode"], self.priced_item)

		order = quotation_api.convert_to_order(quotation["id"], expected_dispatch=DISPATCH)

		order_doc = frappe.get_doc("Sales Order", order["id"])
		self.assertEqual(order_doc.customer_address, self.billing_address)
		self.assertEqual(order_doc.shipping_address_name, other_shipping)
