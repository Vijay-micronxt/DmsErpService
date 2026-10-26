import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.auth.utils import resolve_app_roles
from dms_erp.catalog.setup import setup_catalog
from dms_erp.dashboard import api as dashboard_api
from dms_erp.finance import claims_api, labour_api, unloading_api
from dms_erp.setup.install import create_app_roles
from dms_erp.warehouse.inward_api import add_truck
from dms_erp.warehouse.test_fixtures import ensure_company, make_item, make_supplier


def _user(email, role):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0, "roles": [{"role": role}]}
		).insert(ignore_permissions=True)
	return email


class TestFinanceRole(FrappeTestCase):
	"""Pacific, 26 Oct 2026: a DMS Finance role for BRD C.12.1's Finance dashboard and the
	payment-side actions -- without the warehouse's data-entry rights."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		create_app_roles()
		cls.supplier = make_supplier("Finance Role Test Supplier")
		cls.item = make_item("FIN-ROLE-ITEM", "Vitrified")
		cls.finance_user = _user("finance.tester@pacific.test", "DMS Finance")
		cls.sales_user = _user("finance.sales.tester@pacific.test", "DMS Sales")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_finance_is_a_staff_app_role(self):
		self.assertEqual(resolve_app_roles(["DMS Finance"]), ["finance"])
		self.assertEqual(resolve_app_roles(["DMS Finance", "DMS Management"]), ["management", "finance"])

	def test_finance_dashboard_reports_real_figures(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=100, lr_number="LR-FIN-1")
		unloading_api.record_charge(inward_truck=truck["id"], contractor="Finance Test Contractor", rate_per_box=5, payment_mode="Cash")

		frappe.set_user(self.finance_user)
		data = dashboard_api.finance_dashboard()
		self.assertGreaterEqual(data["unloadingUnpaid"]["count"], 1)
		self.assertGreaterEqual(data["unloadingUnpaid"]["amount"], 500)
		for key in ("receivables", "advancePending", "claimsReceivable", "labourDue"):
			self.assertIn(key, data)

	def test_other_staff_roles_cannot_open_the_finance_dashboard(self):
		frappe.set_user(self.sales_user)
		with self.assertRaises(frappe.PermissionError):
			dashboard_api.finance_dashboard()

	def test_finance_can_pay_but_not_record_charges_or_attendance(self):
		truck = add_truck(supplier=self.supplier, item=self.item, boxes=40, lr_number="LR-FIN-2")
		charge = unloading_api.record_charge(inward_truck=truck["id"], contractor="Finance Test Contractor", rate_per_box=5, payment_mode="Cash")
		record = labour_api.create_labour_record(labourer_name="Finance Test Labourer", daily_rate=500)
		record = labour_api.add_attendance_day(record["id"], date="2026-10-01", present=True)
		claim = claims_api.file_claim(claim_amount=1200, claim_type="Shortage", supplier=self.supplier, responsibility="Factory")

		frappe.set_user(self.finance_user)
		self.assertEqual(unloading_api.mark_paid(charge["id"])["status"], "Paid")
		self.assertEqual(labour_api.record_payment(record["id"], amount_paid=500, payment_mode="Cash")["status"], "Paid")
		self.assertEqual(claims_api.update_claim_status(claim["id"], "Approved")["status"], "Approved")

		# Recording stays with the warehouse.
		with self.assertRaises(frappe.PermissionError):
			unloading_api.record_charge(inward_truck=truck["id"], contractor="X", rate_per_box=5, payment_mode="Cash")
		with self.assertRaises(frappe.PermissionError):
			labour_api.create_labour_record(labourer_name="Not Allowed", daily_rate=500)
		with self.assertRaises(frappe.PermissionError):
			claims_api.file_claim(claim_amount=10, claim_type="Shortage", supplier=self.supplier, responsibility="Factory")
