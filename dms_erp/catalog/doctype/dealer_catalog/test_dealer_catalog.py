import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import dealer_catalog_api as api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing


def make_item(item_code, item_group="Vitrified"):
	if frappe.db.exists("Item", item_code):
		frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": item_group,
			"stock_uom": "Box",
			"is_stock_item": 1,
		}
	).insert(ignore_permissions=True)


def make_dealer(customer_name):
	if frappe.db.exists("Customer", customer_name):
		return customer_name
	frappe.get_doc(
		{"doctype": "Customer", "customer_name": customer_name, "customer_group": "All Customer Groups", "territory": "All Territories"}
	).insert(ignore_permissions=True)
	return customer_name


def make_supplier(supplier_name):
	if frappe.db.exists("Supplier", supplier_name):
		return supplier_name
	frappe.get_doc(
		{"doctype": "Supplier", "supplier_name": supplier_name, "supplier_group": "All Supplier Groups"}
	).insert(ignore_permissions=True)
	return supplier_name


class TestDealerCatalog(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup_catalog()
		setup_pricing()
		cls.item_a = "DCAT-TEST-A"
		cls.item_b = "DCAT-TEST-B"
		make_item(cls.item_a, "Vitrified")
		make_item(cls.item_b, "Wall Tiles")
		cls.dealer = make_dealer("Dealer Catalog Test Dealer")
		cls.supplier = make_supplier("Dealer Catalog Test Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("Dealer Catalog", self.dealer):
			frappe.delete_doc("Dealer Catalog", self.dealer, force=True, ignore_permissions=True)

	def test_unassigned_dealer_falls_back_to_full_catalog(self):
		self.assertTrue(api.is_visible(self.dealer, self.item_a))
		self.assertIn(self.item_a, api.catalog_for(self.dealer))

	def test_set_product_visibility_creates_restricted_catalog(self):
		api.set_product_visibility(self.dealer, self.item_a, True)

		self.assertTrue(api.is_visible(self.dealer, self.item_a))
		self.assertFalse(api.is_visible(self.dealer, self.item_b))
		self.assertEqual(api.catalog_for(self.dealer), [self.item_a])

		api.set_product_visibility(self.dealer, self.item_a, False)
		self.assertFalse(api.is_visible(self.dealer, self.item_a))

	def test_set_category_visibility_bulk_toggles(self):
		api.set_category_visibility(self.dealer, "Vitrified", True)
		coverage = api.category_coverage(self.dealer, "Vitrified")
		self.assertEqual(coverage["visible"], coverage["total"])
		self.assertTrue(api.is_visible(self.dealer, self.item_a))
		self.assertFalse(api.is_visible(self.dealer, self.item_b))

	def test_write_requires_purchase_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.set_product_visibility(self.dealer, self.item_a, True)

	def test_catalog_for_excludes_pulled_back_items_even_when_visible(self):
		api.set_product_visibility(self.dealer, self.item_a, True)
		api.set_product_visibility(self.dealer, self.item_b, True)
		frappe.db.set_value("Item", self.item_b, "custom_discontinuation_status", "Pulled Back")
		try:
			catalog = api.catalog_for(self.dealer)
			self.assertIn(self.item_a, catalog)
			self.assertNotIn(self.item_b, catalog)
			# is_visible is a pure assignment check — unaffected by sellability.
			self.assertTrue(api.is_visible(self.dealer, self.item_b))
		finally:
			frappe.db.set_value("Item", self.item_b, "custom_discontinuation_status", "Active")

	def test_catalog_for_excludes_pulled_back_items_for_unassigned_dealer(self):
		frappe.db.set_value("Item", self.item_a, "custom_discontinuation_status", "Pulled Back")
		try:
			self.assertNotIn(self.item_a, api.catalog_for(self.dealer))
		finally:
			frappe.db.set_value("Item", self.item_a, "custom_discontinuation_status", "Active")

	def test_dealer_catalog_export_lists_only_visible_items_with_price(self):
		pricing_api.ensure_price_record(self.item_a, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=self.item_a, final_price=360, reason="Launch")
		api.set_product_visibility(self.dealer, self.item_a, True)

		html = api.dealer_catalog_export(self.dealer, include_price=True)

		self.assertIn(self.item_a, html)
		self.assertNotIn(self.item_b, html)
		self.assertIn("₹360.00", html)
		self.assertIn("price-inclusive format", html)

	def test_dealer_catalog_export_can_omit_price(self):
		pricing_api.ensure_price_record(self.item_a, self.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=self.item_a, final_price=360, reason="Launch")
		api.set_product_visibility(self.dealer, self.item_a, True)

		html = api.dealer_catalog_export(self.dealer, include_price=False)

		self.assertIn(self.item_a, html)
		self.assertNotIn("₹360.00", html)
		self.assertIn("price-exclusive format", html)

	def test_dealer_catalog_export_includes_the_dealers_own_item_code(self):
		from dms_erp.catalog.api import update_product

		api.set_product_visibility(self.dealer, self.item_a, True)
		# update_product's dealerCodes patch takes the child table's own snake_case row
		# shape directly (dealer/customer_item_code/sample_issued), not the camelCase
		# shape get_product/update_product otherwise return.
		update_product(
			self.item_a,
			{"dealerCodes": [{"dealer": self.dealer, "customer_item_code": "DLR-CODE-1", "sample_issued": False}]},
		)

		html = api.dealer_catalog_export(self.dealer, include_price=False)

		self.assertIn("DLR-CODE-1", html)

	def test_dealer_catalog_export_escapes_item_name(self):
		frappe.db.set_value("Item", self.item_a, "item_name", "Statuario <script>alert(1)</script>")
		api.set_product_visibility(self.dealer, self.item_a, True)

		html = api.dealer_catalog_export(self.dealer, include_price=False)

		self.assertNotIn("<script>", html)
		self.assertIn("&lt;script&gt;", html)

	def test_dealer_catalog_export_handles_an_empty_catalog(self):
		# an actually-empty (not merely unassigned) Dealer Catalog -- add then remove
		# the one item so the record exists with items=[], unlike the fallback-to-full
		# behavior an unassigned dealer gets.
		api.set_product_visibility(self.dealer, self.item_a, True)
		api.set_product_visibility(self.dealer, self.item_a, False)

		html = api.dealer_catalog_export(self.dealer, include_price=True)

		self.assertIn("No items are currently visible", html)
