import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import dealer_catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.purchase.setup import setup_purchase
from dms_erp.sales import inquiry_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestInquiryApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_purchase()
		cls.item = make_item("INQ-TEST-ITEM", "Vitrified")
		cls.dealer = make_dealer("Inquiry Test Dealer")
		cls.supplier = make_supplier("Inquiry Test Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_inquiry_defaults_to_open(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=100, source="WhatsApp")
		self.assertEqual(inquiry["status"], "Open")
		self.assertEqual(inquiry["dealerId"], self.dealer)
		self.assertEqual(inquiry["qty"], 100)

	def test_update_inquiry_patches_status_and_remarks(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=50, source="Phone")
		updated = inquiry_api.update_inquiry(inquiry["id"], {"status": "Available", "remarks": "Confirmed in stock"})
		self.assertEqual(updated["status"], "Available")
		self.assertEqual(updated["remarks"], "Confirmed in stock")

	def test_write_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")

	def test_convert_to_purchase_requirement_creates_po_and_maps_inquiry(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=40, source="Phone")
		inquiry_api.update_inquiry(inquiry["id"], {"status": "Out of Stock"})

		po = inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], supplier=self.supplier, expected_ready_date="2026-09-01")

		self.assertEqual(po["lines"][0]["orderedQty"], 40)
		self.assertEqual(po["sourceInquiry"], inquiry["id"])
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Mapped to PO")

	def test_convert_to_purchase_requirement_rejects_ineligible_status(self):
		inquiry = inquiry_api.create_inquiry(dealer=self.dealer, item=self.item, qty=10, source="Phone")
		inquiry_api.update_inquiry(inquiry["id"], {"status": "Converted to Order"})

		with self.assertRaises(frappe.ValidationError):
			inquiry_api.convert_to_purchase_requirement(inquiry=inquiry["id"], supplier=self.supplier, expected_ready_date="2026-09-01")

	def test_create_inquiry_rejects_item_outside_dealer_catalog(self):
		restricted_item = make_item("INQ-RESTRICTED-ITEM", "Vitrified")
		other_dealer = make_dealer("Inquiry Test Dealer 2")
		# Assigning anything at all to other_dealer creates its Dealer Catalog record,
		# which switches it off the unassigned-dealer full-catalog fallback.
		dealer_catalog_api.set_product_visibility(other_dealer, self.item, True)

		with self.assertRaises(frappe.PermissionError):
			inquiry_api.create_inquiry(dealer=other_dealer, item=restricted_item, qty=5, source="Phone")

	def test_create_inquiry_rejects_pulled_back_item(self):
		item = make_item("INQ-PULLED-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_discontinuation_status", "Pulled Back")

		with self.assertRaises(frappe.ValidationError):
			inquiry_api.create_inquiry(dealer=self.dealer, item=item, qty=5, source="Phone")
