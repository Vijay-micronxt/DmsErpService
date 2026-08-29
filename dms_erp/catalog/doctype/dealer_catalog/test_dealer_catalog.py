import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import dealer_catalog_api as api
from dms_erp.catalog.setup import setup_catalog


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


class TestDealerCatalog(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup_catalog()
		cls.item_a = "DCAT-TEST-A"
		cls.item_b = "DCAT-TEST-B"
		make_item(cls.item_a, "Vitrified")
		make_item(cls.item_b, "Wall Tiles")
		cls.dealer = make_dealer("Dealer Catalog Test Dealer")

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
