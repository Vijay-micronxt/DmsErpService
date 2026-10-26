import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_first_day, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import dealer_api, order_api, sales_target_api
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_item,
	make_supplier,
)

DISPATCH = str(add_days(today(), 30))
THIS_MONTH = str(get_first_day(today()))


def _user(email):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0, "roles": [{"role": "DMS Sales"}]}
		).insert(ignore_permissions=True)
	return email


class TestSalesTargetApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Target Test Supplier")
		cls.salesperson = _user("target-test-salesperson@example.com")
		cls.other_salesperson = _user("target-test-other-salesperson@example.com")
		cls.dealer = make_dealer("Target Test Dealer")
		dealer_api.set_dealer_salesperson(cls.dealer, cls.salesperson)
		cls.item = make_item("TARGET-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_set_salesperson_target_creates_then_upserts(self):
		created = sales_target_api.set_salesperson_target(self.salesperson, THIS_MONTH, 500000)
		self.assertEqual(created["targetAmount"], 500000)
		self.assertEqual(created["month"], THIS_MONTH)

		updated = sales_target_api.set_salesperson_target(self.salesperson, THIS_MONTH, 750000)
		self.assertEqual(updated["id"], created["id"])
		self.assertEqual(updated["targetAmount"], 750000)

		targets = sales_target_api.list_salesperson_targets(THIS_MONTH)
		self.assertEqual(len([t for t in targets if t["salesperson"] == self.salesperson]), 1)

	def test_set_salesperson_target_requires_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			sales_target_api.set_salesperson_target(self.salesperson, THIS_MONTH, 100000)

	def test_set_salesperson_target_rejects_negative_amount(self):
		with self.assertRaises(frappe.ValidationError):
			sales_target_api.set_salesperson_target(self.salesperson, THIS_MONTH, -1)

	def test_progress_measures_actual_order_value_against_target(self):
		sales_target_api.set_salesperson_target(self.salesperson, THIS_MONTH, 1000)
		order_api.create_order(dealer=self.dealer, lines=[{"item": self.item, "qty": 10}], expected_dispatch=DISPATCH)

		rows = sales_target_api.salesperson_target_progress(THIS_MONTH)
		mine = next(r for r in rows if r["salesperson"] == self.salesperson)
		self.assertEqual(mine["actualValue"], 3600)
		self.assertEqual(mine["targetAmount"], 1000)
		self.assertEqual(mine["achievedPct"], 360.0)
		self.assertEqual(mine["dealerCount"], 1)

	def test_progress_omits_salespeople_with_no_target_set(self):
		other_dealer = make_dealer("Target Test Dealer No Target")
		dealer_api.set_dealer_salesperson(other_dealer, self.other_salesperson)
		order_api.create_order(dealer=other_dealer, lines=[{"item": self.item, "qty": 5}], expected_dispatch=DISPATCH)

		rows = sales_target_api.salesperson_target_progress(THIS_MONTH)
		self.assertNotIn(self.other_salesperson, [r["salesperson"] for r in rows])

	def test_progress_zero_target_does_not_divide_by_zero(self):
		zero_person_dealer = make_dealer("Target Test Dealer Zero")
		dealer_api.set_dealer_salesperson(zero_person_dealer, self.salesperson)
		sales_target_api.set_salesperson_target(self.salesperson, THIS_MONTH, 0)

		rows = sales_target_api.salesperson_target_progress(THIS_MONTH)
		mine = next(r for r in rows if r["salesperson"] == self.salesperson)
		self.assertIsNone(mine["achievedPct"])
