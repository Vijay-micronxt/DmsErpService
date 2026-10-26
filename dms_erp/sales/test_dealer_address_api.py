import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.sales import dealer_address_api
from dms_erp.sales.utils import assert_address_belongs_to
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer


class TestDealerAddressApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		cls.dealer = make_dealer("Dealer Address Test Dealer")
		cls.other_dealer = make_dealer("Dealer Address Other Dealer")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_dealer_address_creates_a_real_address_linked_to_the_customer(self):
		address = dealer_address_api.create_dealer_address(
			dealer=self.dealer,
			address_type="Billing",
			address_line1="12 Industrial Estate",
			city="Morbi",
			state="Gujarat",
			pincode="363641",
			is_primary=True,
		)
		self.assertEqual(address["addressType"], "Billing")
		self.assertEqual(address["city"], "Morbi")
		self.assertEqual(address["state"], "Gujarat")
		self.assertEqual(address["pincode"], "363641")
		self.assertTrue(address["isPrimary"])
		self.assertEqual(
			frappe.db.get_value(
				"Dynamic Link", {"parent": address["id"], "parenttype": "Address"}, ["link_doctype", "link_name"]
			),
			("Customer", self.dealer),
		)

	def test_create_dealer_address_accepts_shipping_type_and_flags_is_shipping(self):
		address = dealer_address_api.create_dealer_address(
			dealer=self.dealer,
			address_type="Shipping",
			address_line1="45 Site Road",
			city="Bengaluru",
			state="Karnataka",
			pincode="560001",
			is_primary=True,
		)
		self.assertEqual(address["addressType"], "Shipping")
		self.assertTrue(address["isShipping"])

	def test_create_dealer_address_rejects_an_invalid_address_type(self):
		with self.assertRaises(frappe.ValidationError):
			dealer_address_api.create_dealer_address(
				dealer=self.dealer,
				address_type="Office",
				address_line1="1 Somewhere",
				city="Morbi",
				state="Gujarat",
				pincode="363641",
			)

	def test_create_dealer_address_rejects_missing_required_fields(self):
		with self.assertRaises(frappe.ValidationError):
			dealer_address_api.create_dealer_address(
				dealer=self.dealer, address_type="Billing", address_line1="", city="Morbi", state="Gujarat", pincode="363641"
			)

	def test_list_dealer_addresses_returns_every_address_for_that_dealer_only(self):
		dealer_address_api.create_dealer_address(
			dealer=self.dealer, address_type="Billing", address_line1="1 A St", city="Morbi", state="Gujarat", pincode="363641"
		)
		dealer_address_api.create_dealer_address(
			dealer=self.dealer, address_type="Shipping", address_line1="2 B St", city="Morbi", state="Gujarat", pincode="363641"
		)
		dealer_address_api.create_dealer_address(
			dealer=self.other_dealer, address_type="Billing", address_line1="9 Other St", city="Rajkot", state="Gujarat", pincode="360001"
		)

		listed = dealer_address_api.list_dealer_addresses(self.dealer)
		self.assertEqual(len(listed), 2)
		self.assertEqual({a["addressType"] for a in listed}, {"Billing", "Shipping"})

	def test_update_dealer_address_changes_fields_and_primary_flag(self):
		address = dealer_address_api.create_dealer_address(
			dealer=self.dealer, address_type="Billing", address_line1="Old Line", city="Morbi", state="Gujarat", pincode="363641"
		)
		updated = dealer_address_api.update_dealer_address(address["id"], address_line1="New Line", is_primary=True)
		self.assertEqual(updated["addressLine1"], "New Line")
		self.assertTrue(updated["isPrimary"])

	def test_write_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			dealer_address_api.create_dealer_address(
				dealer=self.dealer, address_type="Billing", address_line1="1 Guest St", city="Morbi", state="Gujarat", pincode="363641"
			)

	def test_assert_address_belongs_to_rejects_another_dealers_address(self):
		other_address = dealer_address_api.create_dealer_address(
			dealer=self.other_dealer,
			address_type="Billing",
			address_line1="9 Other St",
			city="Rajkot",
			state="Gujarat",
			pincode="360001",
		)
		with self.assertRaises(frappe.ValidationError):
			assert_address_belongs_to(other_address["id"], "Customer", self.dealer)

	def test_assert_address_belongs_to_accepts_the_owning_dealers_own_address(self):
		address = dealer_address_api.create_dealer_address(
			dealer=self.dealer, address_type="Billing", address_line1="1 Mine St", city="Morbi", state="Gujarat", pincode="363641"
		)
		assert_address_belongs_to(address["id"], "Customer", self.dealer)  # no exception
