from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import api as catalog_api
from dms_erp.catalog import series_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing


def make_supplier(supplier_name):
	if frappe.db.exists("Supplier", supplier_name):
		return supplier_name
	frappe.get_doc(
		{"doctype": "Supplier", "supplier_name": supplier_name, "supplier_group": "All Supplier Groups"}
	).insert(ignore_permissions=True)
	return supplier_name


def make_dealer(customer_name):
	if frappe.db.exists("Customer", customer_name):
		return customer_name
	frappe.get_doc(
		{"doctype": "Customer", "customer_name": customer_name, "customer_group": "All Customer Groups", "territory": "All Territories"}
	).insert(ignore_permissions=True)
	return customer_name


class TestProducts(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Product Test Supplier")
		cls.dealer = make_dealer("Product Test Dealer")
		# A default Series for tests that aren't themselves exercising series-related
		# behavior -- create_product now requires one (BRD C.1.1). Kept distinct from
		# "Product Test Series" below, which individual tests create/delete locally
		# with their own attributes to test the fill-in/override behavior itself.
		cls.series = series_api.create_series(series_name="Product Test Default Series", finish="Glossy", pieces_per_box=2)["id"]

	@classmethod
	def tearDownClass(cls):
		if frappe.db.exists("Product Series", "Product Test Default Series"):
			frappe.delete_doc("Product Series", "Product Test Default Series", force=True, ignore_permissions=True)
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")
		for code in (
			"PROD-TEST-A",
			"PROD-TEST-B",
			"PROD-TEST-C",
			"PROD-TEST-D",
			"PROD-TEST-IMG-1",
			"PROD-TEST-IMG-2",
			"PROD-TEST-SUPPLIER",
		):
			if frappe.db.exists("Item Price Proposal", code):
				frappe.delete_doc("Item Price Proposal", code, force=True, ignore_permissions=True)
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
		if frappe.db.exists("Product Series", "Product Test Series"):
			frappe.delete_doc("Product Series", "Product Test Series", force=True, ignore_permissions=True)

	def test_create_product_creates_item_and_pending_price_proposal(self):
		product = catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
			size="600x1200mm",
			status="Active",
			pieces_per_box=2,
			sqft_per_box=15.5,
			weight_per_box_kg=28,
			lead_time_days=15,
		)

		self.assertEqual(product["code"], "PROD-TEST-A")
		self.assertIsNone(product["dealerPrice"])  # not approved yet
		self.assertTrue(product["isReorderable"])
		self.assertTrue(product["isSellable"])

		price_record = pricing_api.get_price_record("PROD-TEST-A")
		self.assertEqual(price_record["status"], "Pending")

	def test_hsn_code_passes_through_on_create_and_update(self):
		# gst_hsn_code only exists on Item when india_compliance is installed --
		# not the case in this test environment, so this only verifies our own
		# code threads the value through correctly, not that india_compliance's
		# own validation accepts/requires it (untestable here either way).
		product = catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
			hsn_code="69072100",
		)
		self.assertEqual(product["hsnCode"], "69072100")

		updated = catalog_api.update_product("PROD-TEST-A", {"hsnCode": "69072200"})
		self.assertEqual(updated["hsnCode"], "69072200")

	def test_list_item_groups_returns_seeded_leaf_categories_only(self):
		result = catalog_api.list_item_groups()
		names = [g["name"] for g in result["items"]]
		self.assertIn("Vitrified", names)
		self.assertIn("Floor Tiles", names)
		self.assertNotIn("All Item Groups", names)
		self.assertEqual(result["total"], len(names))

		vitrified = next(g for g in result["items"] if g["name"] == "Vitrified")
		self.assertEqual(vitrified["id"], "Vitrified")
		self.assertIsNotNone(vitrified["parentItemGroup"])

	def test_list_item_groups_is_paginated_and_searchable(self):
		page = catalog_api.list_item_groups(limit=2, offset=0)
		self.assertEqual(len(page["items"]), 2)
		self.assertGreaterEqual(page["total"], 4)  # the 4 seeded groups, at least

		found = catalog_api.list_item_groups(search="Vitrified")
		self.assertEqual(found["total"], 1)
		self.assertEqual(found["items"][0]["name"], "Vitrified")

	def test_list_products_is_paginated_and_searchable(self):
		catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		catalog_api.create_product(
			code="PROD-TEST-B",
			name="Another Product",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		page = catalog_api.list_products(limit=1, offset=0)
		self.assertEqual(len(page["items"]), 1)
		self.assertGreaterEqual(page["total"], 2)

		found = catalog_api.list_products(search="Test Marble Look")
		self.assertEqual(found["total"], 1)
		self.assertEqual(found["items"][0]["code"], "PROD-TEST-A")

		all_products = catalog_api.list_all_products()
		self.assertGreaterEqual(len(all_products), 2)

	def test_list_products_filters_by_category_and_status(self):
		catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		catalog_api.create_product(
			code="PROD-TEST-B",
			name="Another Product",
			category="Floor Tiles",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		catalog_api.update_product("PROD-TEST-B", {"status": "Pulled Back"})

		vitrified_only = catalog_api.list_products(category="Vitrified")
		codes = {p["code"] for p in vitrified_only["items"]}
		self.assertIn("PROD-TEST-A", codes)
		self.assertNotIn("PROD-TEST-B", codes)

		pulled_back_only = catalog_api.list_products(status="Pulled Back")
		codes = {p["code"] for p in pulled_back_only["items"]}
		self.assertIn("PROD-TEST-B", codes)
		self.assertNotIn("PROD-TEST-A", codes)

	def test_list_products_filters_by_supplier(self):
		other_supplier = make_supplier("Product Test Supplier Two")
		catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		catalog_api.create_product(
			code="PROD-TEST-B",
			name="Another Product",
			category="Vitrified",
			supplier=other_supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		by_first_supplier = catalog_api.list_products(supplier=self.supplier)
		codes = {p["code"] for p in by_first_supplier["items"]}
		self.assertIn("PROD-TEST-A", codes)
		self.assertNotIn("PROD-TEST-B", codes)

		by_second_supplier = catalog_api.list_products(supplier=other_supplier)
		codes = {p["code"] for p in by_second_supplier["items"]}
		self.assertIn("PROD-TEST-B", codes)
		self.assertNotIn("PROD-TEST-A", codes)

	def test_create_product_requires_purchase_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			catalog_api.create_product(
				code="PROD-TEST-B",
				name="Blocked",
				category="Vitrified",
				supplier=self.supplier,
				purchase_cost=100,
				margin_pct=20,
				effective_date="2026-08-01",
				series_ref=self.series,
			)

	def test_create_product_requires_a_series_ref(self):
		with self.assertRaises(frappe.ValidationError):
			catalog_api.create_product(
				code="PROD-TEST-B",
				name="No Series",
				category="Vitrified",
				supplier=self.supplier,
				purchase_cost=100,
				margin_pct=20,
				effective_date="2026-08-01",
			)

		with self.assertRaises(frappe.ValidationError):
			catalog_api.create_product(
				code="PROD-TEST-B",
				name="Bogus Series",
				category="Vitrified",
				supplier=self.supplier,
				purchase_cost=100,
				margin_pct=20,
				effective_date="2026-08-01",
				series_ref="No Such Series",
			)

	def test_update_product_status_changes_lifecycle_flags(self):
		catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		updated = catalog_api.update_product("PROD-TEST-A", {"status": "Pulled Back"})
		self.assertFalse(updated["isReorderable"])
		self.assertFalse(updated["isSellable"])

	def test_approved_price_flows_into_product_listing(self):
		catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		pricing_api.approve_price(item="PROD-TEST-A", final_price=612, reason="Launch")

		product = catalog_api.get_product("PROD-TEST-A")
		self.assertEqual(product["dealerPrice"], 612)

	def test_new_product_has_no_dealer_codes_or_images(self):
		product = catalog_api.create_product(
			code="PROD-TEST-C",
			name="Test Codes/Images Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		self.assertEqual(product["dealerCodes"], [])
		self.assertEqual(product["images"], [])

	def test_update_product_sets_dealer_codes_and_images(self):
		catalog_api.create_product(
			code="PROD-TEST-C",
			name="Test Codes/Images Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		updated = catalog_api.update_product(
			"PROD-TEST-C",
			{
				"dealerCodes": [{"dealer": self.dealer, "customer_item_code": "DLR-CODE-1", "sample_issued": 1}],
				"images": [{"image": "/files/tile.jpg", "image_type": "Product", "is_primary": 1}],
			},
		)

		self.assertEqual(
			updated["dealerCodes"], [{"dealer": self.dealer, "customerItemCode": "DLR-CODE-1", "sampleIssued": True}]
		)
		self.assertEqual(updated["images"], [{"image": "/files/tile.jpg", "imageType": "Product", "isPrimary": True}])

	def test_resolve_dealer_code_finds_the_item_by_the_dealers_own_code(self):
		catalog_api.create_product(
			code="PROD-TEST-C",
			name="Test Codes/Images Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		catalog_api.update_product(
			"PROD-TEST-C", {"dealerCodes": [{"dealer": self.dealer, "customer_item_code": "DLR-CODE-2", "sample_issued": 1}]}
		)

		resolved = catalog_api.resolve_dealer_code(self.dealer, "DLR-CODE-2")
		self.assertEqual(resolved["id"], "PROD-TEST-C")

	def test_resolve_dealer_code_returns_none_for_an_unknown_code(self):
		self.assertIsNone(catalog_api.resolve_dealer_code(self.dealer, "NO-SUCH-CODE"))

	def test_resolve_item_mention_finds_the_code_inside_a_free_text_message(self):
		catalog_api.create_product(
			code="PROD-TEST-M",
			name="Test Mention Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		catalog_api.update_product(
			"PROD-TEST-M", {"dealerCodes": [{"dealer": self.dealer, "customer_item_code": "GVT-6013", "sample_issued": 1}]}
		)

		# Typed exactly as stored, embedded in a sentence.
		self.assertEqual(catalog_api.resolve_item_mention(self.dealer, "GVT-6013 stock hai kya")["id"], "PROD-TEST-M")
		# Space-separated instead of hyphenated -- still resolves via the joined candidates.
		self.assertEqual(catalog_api.resolve_item_mention(self.dealer, "GVT 6013 available?")["id"], "PROD-TEST-M")
		# Concatenated, no separator at all -- recovered via the letter/digit boundary split.
		self.assertEqual(catalog_api.resolve_item_mention(self.dealer, "any GVT6013 in stock")["id"], "PROD-TEST-M")
		# Lowercase, as typed on a phone keyboard.
		self.assertEqual(catalog_api.resolve_item_mention(self.dealer, "gvt6013 milega kya")["id"], "PROD-TEST-M")

	def test_resolve_item_mention_returns_none_when_no_known_code_appears(self):
		self.assertIsNone(catalog_api.resolve_item_mention(self.dealer, "bhai kal wale rate wapas bhej do"))
		self.assertIsNone(catalog_api.resolve_item_mention(self.dealer, ""))

	def test_create_product_with_series_ref_fills_in_unset_attributes(self):
		series_api.create_series(
			series_name="Product Test Series",
			finish="Glossy",
			pieces_per_box=2,
			sqft_per_box=15.5,
			weight_per_box_kg=28,
			bulk_qty_threshold=200,
			retail_qty_threshold=50,
		)

		product = catalog_api.create_product(
			code="PROD-TEST-D",
			name="Series-Backed Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref="Product Test Series",
		)

		self.assertEqual(product["seriesRef"], "Product Test Series")
		self.assertEqual(product["series"], "Product Test Series")
		self.assertEqual(product["finish"], "Glossy")
		self.assertEqual(product["piecesPerBox"], 2)
		self.assertEqual(product["sqftPerBox"], 15.5)
		self.assertEqual(product["weightPerBoxKg"], 28)
		self.assertEqual(product["bulkQtyThreshold"], 200)
		self.assertEqual(product["retailQtyThreshold"], 50)

	def test_create_product_explicit_attributes_override_the_series(self):
		series_api.create_series(series_name="Product Test Series", finish="Glossy", pieces_per_box=2)

		product = catalog_api.create_product(
			code="PROD-TEST-D",
			name="Series-Backed Item, Overridden",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref="Product Test Series",
			finish="Matte",
			pieces_per_box=4,
		)

		self.assertEqual(product["finish"], "Matte")
		self.assertEqual(product["piecesPerBox"], 4)

	def test_create_product_sets_the_launch_supplier_as_the_default_supplier(self):
		product = catalog_api.create_product(
			code="PROD-TEST-SUPPLIER",
			name="Default Supplier Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		self.assertEqual(product["defaultSupplier"], self.supplier)

	def test_create_product_accepts_a_default_supplier_independent_of_the_launch_supplier(self):
		other_supplier = make_supplier("Product Test Other Launch Supplier")
		product = catalog_api.create_product(
			code="PROD-TEST-SUPPLIER",
			name="Independently Sourced Item",
			category="Vitrified",
			supplier=self.supplier,
			default_supplier=other_supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		self.assertEqual(product["defaultSupplier"], other_supplier)
		# The launch supplier still seeded the pricing proposal, unaffected by default_supplier.
		price_record = pricing_api.get_price_record("PROD-TEST-SUPPLIER")
		self.assertEqual(price_record["supplier"], self.supplier)

	def test_default_supplier_falls_back_to_the_series_supplier(self):
		series_api.create_series(series_name="Product Test Series", supplier=self.supplier)
		catalog_api.create_product(
			code="PROD-TEST-SUPPLIER",
			name="Series Default Supplier Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref="Product Test Series",
		)
		# Simulates an item created before custom_default_supplier existed (or one
		# never given its own override) -- the Series it's linked to should still
		# resolve a default.
		frappe.db.set_value("Item", "PROD-TEST-SUPPLIER", "custom_default_supplier", None)

		product = catalog_api.get_product("PROD-TEST-SUPPLIER")
		self.assertEqual(product["defaultSupplier"], self.supplier)

	def test_update_product_overrides_the_default_supplier(self):
		catalog_api.create_product(
			code="PROD-TEST-SUPPLIER",
			name="Overridden Supplier Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		other_supplier = make_supplier("Product Test Other Supplier")

		updated = catalog_api.update_product("PROD-TEST-SUPPLIER", {"defaultSupplier": other_supplier})
		self.assertEqual(updated["defaultSupplier"], other_supplier)

	def test_update_product_series_ref_rederives_the_label(self):
		other_series = series_api.create_series(series_name="Product Test Other Series", finish="Matte")["id"]
		catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		updated = catalog_api.update_product("PROD-TEST-A", {"seriesRef": other_series})

		self.assertEqual(updated["seriesRef"], other_series)
		self.assertEqual(updated["series"], "Product Test Other Series")

		frappe.delete_doc("Product Series", other_series, force=True, ignore_permissions=True)

	def test_update_product_rejects_an_unknown_series_ref(self):
		catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		with self.assertRaises(frappe.ValidationError):
			catalog_api.update_product("PROD-TEST-A", {"seriesRef": "No Such Series"})

	def _fake_upload(self, filename=b"fake-image-bytes", name="swatch.jpg"):
		fake_file = MagicMock()
		fake_file.filename = name
		fake_file.stream.read.return_value = filename
		return fake_file

	def test_upload_product_image_appends_a_row_and_returns_the_gallery(self):
		catalog_api.create_product(
			code="PROD-TEST-IMG-1",
			name="Image Gallery Test Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		with patch.object(frappe.local, "request") as mock_request:
			mock_request.files = {"file": self._fake_upload()}
			result = catalog_api.upload_product_image(
				item="PROD-TEST-IMG-1", image_type="Application", is_primary="true"
			)

		self.assertEqual(len(result["images"]), 1)
		self.assertEqual(result["images"][0]["imageType"], "Application")
		self.assertTrue(result["images"][0]["isPrimary"])
		self.assertTrue(result["images"][0]["image"])

	def test_upload_product_image_rejects_an_invalid_image_type(self):
		catalog_api.create_product(
			code="PROD-TEST-IMG-1",
			name="Image Gallery Test Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		with patch.object(frappe.local, "request") as mock_request:
			mock_request.files = {"file": self._fake_upload()}
			with self.assertRaises(frappe.ValidationError):
				catalog_api.upload_product_image(item="PROD-TEST-IMG-1", image_type="Bogus")

	def test_upload_product_image_requires_a_file(self):
		catalog_api.create_product(
			code="PROD-TEST-IMG-1",
			name="Image Gallery Test Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)

		with patch.object(frappe.local, "request") as mock_request:
			mock_request.files = {}
			with self.assertRaises(frappe.ValidationError):
				catalog_api.upload_product_image(item="PROD-TEST-IMG-1")

	def test_remove_product_image_drops_only_the_matching_row(self):
		catalog_api.create_product(
			code="PROD-TEST-IMG-2",
			name="Image Removal Test Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		doc = frappe.get_doc("Item", "PROD-TEST-IMG-2")
		doc.append("custom_images", {"image": "/files/a.jpg", "image_type": "Product", "is_primary": 1})
		doc.append("custom_images", {"image": "/files/b.jpg", "image_type": "Application", "is_primary": 0})
		doc.save(ignore_permissions=True)

		result = catalog_api.remove_product_image(item="PROD-TEST-IMG-2", image="/files/a.jpg")

		self.assertEqual(len(result["images"]), 1)
		self.assertEqual(result["images"][0]["image"], "/files/b.jpg")

	def test_upload_and_remove_product_image_require_purchase_or_management_role(self):
		catalog_api.create_product(
			code="PROD-TEST-IMG-1",
			name="Image Gallery Test Item",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
			series_ref=self.series,
		)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			catalog_api.upload_product_image(item="PROD-TEST-IMG-1")
		with self.assertRaises(frappe.PermissionError):
			catalog_api.remove_product_image(item="PROD-TEST-IMG-1", image="/files/a.jpg")
