import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.finance import unloading_api
from dms_erp.warehouse.inward_api import add_truck
from dms_erp.warehouse.test_fixtures import ensure_company, make_item, make_supplier


class TestUnloadingApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		cls.supplier = make_supplier("Unloading Test Supplier")
		cls.item = make_item("UNLOAD-TEST-ITEM", "Vitrified")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_get_charge_for_truck_returns_none_when_unrecorded(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=640, lr_number="LR-UNL-1")
		self.assertIsNone(unloading_api.get_charge_for_truck(truck["id"]))

	def test_record_charge_derives_boxes_and_computes_amount(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=640, lr_number="LR-UNL-2")
		charge = unloading_api.record_charge(
			inward_truck=truck["id"], contractor="Morbi Labour Contractors", rate_per_box=6, payment_mode="Cash"
		)
		self.assertEqual(charge["boxes"], 640)
		self.assertEqual(charge["chargeAmount"], 3840)
		self.assertEqual(charge["status"], "Pending")
		self.assertEqual(charge["lr"], "LR-UNL-2")

	def test_record_charge_rejects_duplicate_for_same_truck(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=100, lr_number="LR-UNL-3")
		unloading_api.record_charge(inward_truck=truck["id"], contractor="Contractor A", rate_per_box=5, payment_mode="Cash")
		with self.assertRaises(frappe.DuplicateEntryError):
			unloading_api.record_charge(inward_truck=truck["id"], contractor="Contractor A", rate_per_box=5, payment_mode="Cash")

	def test_mark_paid_stamps_payer_and_date(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=200, lr_number="LR-UNL-4")
		charge = unloading_api.record_charge(inward_truck=truck["id"], contractor="Contractor B", rate_per_box=6, payment_mode="UPI")

		paid = unloading_api.mark_paid(charge["id"])
		self.assertEqual(paid["status"], "Paid")
		self.assertEqual(paid["paidBy"], "Administrator")
		self.assertIsNotNone(paid["paidAt"])

	def test_write_requires_warehouse_or_management_role(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=50, lr_number="LR-UNL-5")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			unloading_api.record_charge(inward_truck=truck["id"], contractor="Contractor C", rate_per_box=5, payment_mode="Cash")
