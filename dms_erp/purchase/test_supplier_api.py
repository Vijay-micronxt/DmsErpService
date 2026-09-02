import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.purchase import supplier_api
from dms_erp.warehouse.test_fixtures import make_supplier


class TestSupplierApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.supplier = make_supplier("Supplier Api Test Co")

	def test_get_supplier_returns_native_supplier_fields(self):
		result = supplier_api.get_supplier(self.supplier)
		self.assertEqual(result["id"], self.supplier)
		self.assertEqual(result["name"], self.supplier)
		self.assertEqual(result["group"], "All Supplier Groups")
		self.assertFalse(result["disabled"])

	def test_list_suppliers_filters_by_search_and_excludes_disabled_by_default(self):
		results = supplier_api.list_suppliers(search="Supplier Api Test")
		self.assertIn(self.supplier, [r["id"] for r in results])

		frappe.db.set_value("Supplier", self.supplier, "disabled", 1)
		try:
			active = supplier_api.list_suppliers(search="Supplier Api Test")
			self.assertNotIn(self.supplier, [r["id"] for r in active])

			disabled_only = supplier_api.list_suppliers(search="Supplier Api Test", disabled=True)
			self.assertIn(self.supplier, [r["id"] for r in disabled_only])
		finally:
			frappe.db.set_value("Supplier", self.supplier, "disabled", 0)
