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
		# Pacific Accounting Settings is a Single (global state) — never leave it
		# configured for the next test.
		frappe.db.set_single_value("Pacific Accounting Settings", "post_accounting_entries", 0)
		for field in ("default_company", "default_bank_account", "unloading_expense_account"):
			frappe.db.set_single_value("Pacific Accounting Settings", field, None)

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

	def test_mark_paid_never_posts_when_setting_unchecked(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=100, lr_number="LR-UNL-6")
		charge = unloading_api.record_charge(inward_truck=truck["id"], contractor="Contractor D", rate_per_box=5, payment_mode="Cash")

		paid = unloading_api.mark_paid(charge["id"])
		self.assertIsNone(paid["paymentEntry"])

	def test_mark_paid_rejects_when_posting_on_but_accounts_missing(self):
		frappe.db.set_single_value("Pacific Accounting Settings", "post_accounting_entries", 1)

		truck = add_truck(supplier=self.supplier, item=self.item, boxes=100, lr_number="LR-UNL-7")
		charge = unloading_api.record_charge(inward_truck=truck["id"], contractor="Contractor E", rate_per_box=5, payment_mode="Cash")

		with self.assertRaises(frappe.ValidationError):
			unloading_api.mark_paid(charge["id"])
		# Status untouched by the failed posting attempt — the doc.save() never ran.
		self.assertEqual(unloading_api.get_charge_for_truck(truck["id"])["status"], "Pending")

	def test_mark_paid_posts_payment_entry_when_configured(self):
		company = ensure_company()
		accounts = frappe.get_all("Account", filters={"company": company, "is_group": 0}, pluck="name", limit=2)
		if len(accounts) < 2:
			self.skipTest("Test company has no Chart of Accounts to pick two leaf accounts from.")
		bank_account, expense_account = accounts[0], accounts[1]

		frappe.db.set_single_value("Pacific Accounting Settings", "post_accounting_entries", 1)
		frappe.db.set_single_value("Pacific Accounting Settings", "default_company", company)
		frappe.db.set_single_value("Pacific Accounting Settings", "default_bank_account", bank_account)
		frappe.db.set_single_value("Pacific Accounting Settings", "unloading_expense_account", expense_account)

		truck = add_truck(supplier=self.supplier, item=self.item, boxes=100, lr_number="LR-UNL-8")
		charge = unloading_api.record_charge(inward_truck=truck["id"], contractor="Contractor F", rate_per_box=5, payment_mode="Cash")

		paid = unloading_api.mark_paid(charge["id"])
		self.assertIsNotNone(paid["paymentEntry"])
		pe = frappe.get_doc("Payment Entry", paid["paymentEntry"])
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(pe.paid_amount, 500)
		self.assertEqual(pe.paid_from, bank_account)
		self.assertEqual(pe.paid_to, expense_account)

	def test_write_requires_warehouse_or_management_role(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=50, lr_number="LR-UNL-5")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			unloading_api.record_charge(inward_truck=truck["id"], contractor="Contractor C", rate_per_box=5, payment_mode="Cash")
