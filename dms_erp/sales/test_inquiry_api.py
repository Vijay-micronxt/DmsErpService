import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.sales import inquiry_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item


class TestInquiryApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		cls.item = make_item("INQ-TEST-ITEM", "Vitrified")
		cls.dealer = make_dealer("Inquiry Test Dealer")

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
