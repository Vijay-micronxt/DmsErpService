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
