import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import api as catalog_api
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


class TestProducts(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Product Test Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")
		for code in ("PROD-TEST-A", "PROD-TEST-B"):
			if frappe.db.exists("Item Price Proposal", code):
				frappe.delete_doc("Item Price Proposal", code, force=True, ignore_permissions=True)
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, force=True, ignore_permissions=True)

	def test_create_product_creates_item_and_pending_price_proposal(self):
		product = catalog_api.create_product(
			code="PROD-TEST-A",
			name="Test Marble Look",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
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
		)
		catalog_api.create_product(
			code="PROD-TEST-B",
			name="Another Product",
			category="Vitrified",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
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
		)
		catalog_api.create_product(
			code="PROD-TEST-B",
			name="Another Product",
			category="Floor Tiles",
			supplier=self.supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
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
		)
		catalog_api.create_product(
			code="PROD-TEST-B",
			name="Another Product",
			category="Vitrified",
			supplier=other_supplier,
			purchase_cost=400,
			margin_pct=25,
			effective_date="2026-08-01",
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
		)
		pricing_api.approve_price(item="PROD-TEST-A", final_price=612, reason="Launch")

		product = catalog_api.get_product("PROD-TEST-A")
		self.assertEqual(product["dealerPrice"], 612)
