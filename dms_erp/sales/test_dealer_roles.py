import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.auth import dealer_api
from dms_erp.sales import dealer_api as staff_dealer_api
from dms_erp.sales import dealer_portal_api, dealer_roles
from dms_erp.warehouse.test_fixtures import make_dealer

MAIN = "9811100001"
EXTRA = "9811100002"
ALL = list(dealer_roles.PERMISSIONS)


def _tick(role: str, permission: str, on: bool) -> None:
	frappe.db.set_single_value("DMS Sales Settings", dealer_roles.setting_field(role, permission), 1 if on else 0)


class TestDealerRoles(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.dealer = make_dealer("Dealer Roles Test Dealer")

	def setUp(self):
		# Every case starts from the same dealer and the shipped defaults: these tests change roles,
		# numbers and ticks, and a rollback only happens at the end of the whole class.
		doc = frappe.get_doc("Customer", self.dealer)
		doc.custom_phone = MAIN
		doc.custom_phone_role = None
		doc.set("custom_additional_phones", [{"phone": EXTRA, "contact_name": "Sales person", "role": dealer_roles.SALES}])
		doc.save(ignore_permissions=True)
		for role, default in ((dealer_roles.ACCOUNTS, True), (dealer_roles.SALES, False)):
			for permission in ALL:
				_tick(role, permission, default)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _user(self, phone=None):
		return dealer_api._dealer_portal_user(self.dealer, phone)

	def test_each_number_signs_in_as_its_own_user_and_the_main_one_keeps_the_old_account(self):
		main, extra = self._user(MAIN), self._user(EXTRA)
		self.assertNotEqual(main, extra)
		# the main number, with or without the number given, is the one account every dealer already had
		self.assertEqual(self._user(), main)
		self.assertEqual(self._user(f"+91 {MAIN[:5]} {MAIN[5:]}"), main)
		self.assertEqual(self._user(EXTRA), extra)
		self.assertEqual(frappe.db.get_value("User", main, "custom_dealer_phone") or None, None)
		self.assertEqual(frappe.db.get_value("User", extra, "custom_dealer_phone"), EXTRA)

	def test_email_password_sign_in_still_resolves_to_the_main_account(self):
		self._user(EXTRA)
		self.assertEqual(dealer_api._main_portal_user(self.dealer), self._user(MAIN))

	def test_a_blank_role_counts_as_accounts_so_existing_users_keep_everything(self):
		self.assertEqual(dealer_roles.role_for_user(self._user(MAIN)), dealer_roles.ACCOUNTS)
		self.assertEqual(dealer_roles.permissions_for_user(self._user(MAIN)), ALL)

	def test_the_role_on_the_phone_is_read_live(self):
		main = self._user(MAIN)
		frappe.db.set_value("Customer", self.dealer, "custom_phone_role", dealer_roles.SALES)
		self.assertEqual(dealer_roles.role_for_user(main), dealer_roles.SALES)
		frappe.db.set_value("Customer", self.dealer, "custom_phone_role", dealer_roles.ACCOUNTS)
		self.assertEqual(dealer_roles.role_for_user(main), dealer_roles.ACCOUNTS)

	def test_sales_starts_with_nothing_until_ticked(self):
		extra = self._user(EXTRA)
		self.assertEqual(dealer_roles.role_for_user(extra), dealer_roles.SALES)
		self.assertEqual(dealer_roles.permissions_for_user(extra), [])
		_tick(dealer_roles.SALES, "view_quotations", True)
		self.assertEqual(dealer_roles.permissions_for_user(extra), ["view_quotations"])

	def test_unticking_a_permission_takes_it_from_accounts_too(self):
		main = self._user(MAIN)
		_tick(dealer_roles.ACCOUNTS, "view_invoices", False)
		self.assertNotIn("view_invoices", dealer_roles.permissions_for_user(main))
		self.assertIn("view_orders", dealer_roles.permissions_for_user(main))

	def test_a_number_removed_from_the_dealer_stops_having_any_access(self):
		gone = self._user(EXTRA)
		doc = frappe.get_doc("Customer", self.dealer)
		doc.set("custom_additional_phones", [])
		doc.save(ignore_permissions=True)
		self.assertIsNone(dealer_roles.role_for_user(gone))
		self.assertEqual(dealer_roles.permissions_for_user(gone), [])

	def test_a_non_dealer_account_has_no_dealer_permissions(self):
		self.assertEqual(dealer_roles.permissions_for_user("Administrator"), [])

	def test_the_gate_refuses_in_plain_words_without_naming_the_role(self):
		frappe.set_user(self._user(EXTRA))
		with self.assertRaises(frappe.PermissionError) as ctx:
			dealer_roles.require("raise_order")
		message = str(ctx.exception)
		self.assertIn("raise a sales order", message)
		self.assertNotIn("Dealer Sales", message)

	def test_every_dealer_endpoint_is_gated_for_a_role_without_the_permission(self):
		frappe.set_user(self._user(EXTRA))
		calls = {
			"view_quotations": [lambda: dealer_portal_api.list_my_quotations(), lambda: dealer_portal_api.get_my_quotation("X")],
			"view_orders": [lambda: dealer_portal_api.list_my_orders(), lambda: dealer_portal_api.get_my_order("X")],
			"view_invoices": [lambda: dealer_portal_api.list_my_invoices(), lambda: dealer_portal_api.get_my_invoice("X")],
			"raise_order": [
				lambda: dealer_portal_api.convert_to_order("X", "2030-01-01", "PO1"),
				lambda: dealer_portal_api.accept_quotation("X", "2030-01-01", "PO1"),
			],
		}
		for permission, fns in calls.items():
			for fn in fns:
				with self.assertRaises(frappe.PermissionError, msg=permission) as ctx:
					fn()
				self.assertIn("don't have permission", str(ctx.exception), msg=permission)

	def test_ticking_the_permission_lets_the_call_through(self):
		_tick(dealer_roles.SALES, "view_orders", True)
		_tick(dealer_roles.SALES, "view_quotations", True)
		_tick(dealer_roles.SALES, "view_invoices", True)
		frappe.set_user(self._user(EXTRA))
		for fn in (dealer_portal_api.list_my_orders, dealer_portal_api.list_my_quotations, dealer_portal_api.list_my_invoices):
			self.assertEqual(fn()["total"], 0)

	def test_profile_tells_the_portal_what_the_person_may_use(self):
		_tick(dealer_roles.SALES, "view_orders", True)
		frappe.set_user(self._user(EXTRA))
		profile = dealer_portal_api.my_profile()
		self.assertEqual((profile["role"], profile["permissions"]), (dealer_roles.SALES, ["view_orders"]))

	def test_money_is_stripped_at_any_depth_and_other_fields_stay(self):
		data = {
			"id": "SO-1",
			"total": 100,
			"lines": [{"itemCode": "A", "qty": 2, "rate": 50, "amount": 100, "priceListRate": 60}],
			"invoices": [{"id": "SI-1", "grandTotal": 118, "taxes": [{"rate": 18}], "items": [{"qty": 2, "rate": 50}]}],
			"prices": {"row1": 60},
		}
		self.assertEqual(
			dealer_roles.strip_prices(data),
			{"id": "SO-1", "lines": [{"itemCode": "A", "qty": 2}], "invoices": [{"id": "SI-1", "items": [{"qty": 2}]}]},
		)

	def test_price_filtered_follows_the_permission(self):
		_tick(dealer_roles.SALES, "see_price", False)
		frappe.set_user(self._user(EXTRA))
		self.assertEqual(dealer_roles.price_filtered({"qty": 1, "rate": 5}), {"qty": 1})
		frappe.set_user("Administrator")
		_tick(dealer_roles.SALES, "see_price", True)
		frappe.set_user(self._user(EXTRA))
		self.assertEqual(dealer_roles.price_filtered({"qty": 1, "rate": 5}), {"qty": 1, "rate": 5})


class TestStaffSetsDealerRoles(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.dealer = make_dealer("Dealer Roles Staff Dealer")

	def test_phone_roles_round_trip_through_update_dealer(self):
		out = staff_dealer_api.update_dealer(
			self.dealer,
			{
				"phone": "9822200001",
				"phoneRole": dealer_roles.ACCOUNTS,
				"additionalPhones": [{"phone": "9822200002", "name": "Asha", "role": dealer_roles.SALES}, {"phone": "9822200003"}],
			},
		)
		self.assertEqual(out["phoneRole"], dealer_roles.ACCOUNTS)
		self.assertEqual(
			[(p["phone"], p["role"]) for p in out["additionalPhones"]],
			[("9822200002", dealer_roles.SALES), ("9822200003", None)],
		)
		listed = next(d for d in staff_dealer_api.list_dealers() if d["id"] == self.dealer)
		self.assertEqual(listed["additionalPhones"][0]["role"], dealer_roles.SALES)

	def test_an_unknown_role_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			staff_dealer_api.update_dealer(self.dealer, {"phoneRole": "Dealer Boss"})
		with self.assertRaises(frappe.ValidationError):
			staff_dealer_api.update_dealer(self.dealer, {"additionalPhones": [{"phone": "9822200009", "role": "Nope"}]})

	def test_clearing_the_role_goes_back_to_blank(self):
		staff_dealer_api.update_dealer(self.dealer, {"phoneRole": dealer_roles.SALES})
		self.assertIsNone(staff_dealer_api.update_dealer(self.dealer, {"phoneRole": ""})["phoneRole"])
