import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.auth.dealer_api import _dealer_portal_user
from dms_erp.catalog import dealer_catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import dealer_portal_api, inquiry_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier


class TestDealerPortalApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Dealer Portal Test Supplier")

		cls.dealer = make_dealer("Dealer Portal Test Dealer")
		cls.dealer_user = _dealer_portal_user(cls.dealer)
		cls.other_dealer = make_dealer("Dealer Portal Other Dealer")
		cls.other_dealer_user = _dealer_portal_user(cls.other_dealer)

		cls.item = make_item("DP-TEST-ITEM", "Vitrified")
		dealer_catalog_api.set_product_visibility(cls.dealer, cls.item, True)
		dealer_catalog_api.set_product_visibility(cls.other_dealer, cls.item, True)
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		pricing_api.approve_price(item=cls.item, final_price=360, reason="Launch")

		cls.hidden_item = make_item("DP-HIDDEN-ITEM", "Vitrified")
		dealer_catalog_api.set_product_visibility(cls.other_dealer, cls.hidden_item, True)

	def setUp(self):
		frappe.set_user(self.dealer_user)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_get_catalog_only_returns_this_dealers_visible_items(self):
		page = dealer_portal_api.get_catalog()
		codes = [row["code"] for row in page["items"]]
		self.assertIn(self.item, codes)
		self.assertNotIn(self.hidden_item, codes)

	def test_catalog_entry_exposes_only_this_dealers_own_code_not_the_full_list(self):
		from dms_erp.catalog.api import update_product

		frappe.set_user("Administrator")
		update_product(
			self.item,
			{
				"dealerCodes": [
					{"dealer": self.dealer, "customer_item_code": "MY-OWN-CODE", "sample_issued": 1},
					{"dealer": self.other_dealer, "customer_item_code": "THEIR-CODE", "sample_issued": 1},
				]
			},
		)
		frappe.set_user(self.dealer_user)

		entry = dealer_portal_api.get_item(self.item)
		self.assertEqual(entry["dealerCode"], "MY-OWN-CODE")
		self.assertNotIn("dealerCodes", entry)
		self.assertNotIn("THEIR-CODE", str(entry))

	def test_get_item_rejects_an_item_outside_this_dealers_catalog(self):
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_item(self.hidden_item)

	def test_resolve_code_by_the_dealers_own_mapped_code(self):
		from dms_erp.catalog.api import update_product

		frappe.set_user("Administrator")
		update_product(self.item, {"dealerCodes": [{"dealer": self.dealer, "customer_item_code": "DLR-OWN-1", "sample_issued": 0}]})
		frappe.set_user(self.dealer_user)

		found = dealer_portal_api.resolve_code("DLR-OWN-1")
		self.assertEqual(found["code"], self.item)

	def test_resolve_code_falls_back_to_the_company_item_code(self):
		found = dealer_portal_api.resolve_code(self.item)
		self.assertEqual(found["code"], self.item)

	def test_resolve_code_returns_none_for_an_item_outside_the_catalog(self):
		self.assertIsNone(dealer_portal_api.resolve_code(self.hidden_item))

	def test_raise_inquiry_is_web_sourced_and_unassigned(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=20, remarks="Need urgently")
		self.assertEqual(inquiry["source"], "Web")
		self.assertEqual(inquiry["dealerId"], self.dealer)
		self.assertIsNone(inquiry["assignedTo"])

	def test_raise_inquiry_rejects_an_item_outside_the_catalog(self):
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.raise_inquiry(item=self.hidden_item, qty=5)

	def test_convert_to_order_requires_a_customer_po(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch="2026-09-01", customer_po="")

	def test_convert_to_order_sets_po_no_and_closes_the_inquiry(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		order = dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch="2026-09-01", customer_po="PO-DEALER-001")

		self.assertEqual(order["customerPo"], "PO-DEALER-001")
		self.assertEqual(order["dealerId"], self.dealer)
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["status"], "Converted to Order")
		self.assertEqual(inquiry_api.get_inquiry(inquiry["id"])["customerPo"], "PO-DEALER-001")

	def test_convert_to_order_rejects_an_inquiry_that_belongs_to_another_dealer(self):
		frappe.set_user(self.other_dealer_user)
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		frappe.set_user(self.dealer_user)

		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch="2026-09-01", customer_po="PO-X")

	def test_convert_to_order_rejects_an_already_converted_inquiry(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch="2026-09-01", customer_po="PO-1")

		with self.assertRaises(frappe.ValidationError):
			dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch="2026-09-01", customer_po="PO-2")

	def test_list_my_inquiries_never_shows_another_dealers_inquiries(self):
		dealer_portal_api.raise_inquiry(item=self.item, qty=1)
		frappe.set_user(self.other_dealer_user)
		dealer_portal_api.raise_inquiry(item=self.item, qty=2)

		frappe.set_user(self.dealer_user)
		mine = dealer_portal_api.list_my_inquiries()
		self.assertTrue(all(row["dealerId"] == self.dealer for row in mine["items"]))

	def test_list_my_orders_never_shows_another_dealers_orders(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch="2026-09-01", customer_po="PO-MINE")

		frappe.set_user(self.other_dealer_user)
		other_inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		dealer_portal_api.convert_to_order(inquiry=other_inquiry["id"], expected_dispatch="2026-09-01", customer_po="PO-THEIRS")
		mine = dealer_portal_api.list_my_orders()
		self.assertTrue(all(row["dealerId"] == self.other_dealer for row in mine["items"]))

	def test_get_my_order_rejects_another_dealers_order(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=10)
		order = dealer_portal_api.convert_to_order(inquiry=inquiry["id"], expected_dispatch="2026-09-01", customer_po="PO-MINE-2")

		frappe.set_user(self.other_dealer_user)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_order(order["id"])

	def test_get_my_inquiry_rejects_another_dealers_inquiry(self):
		inquiry = dealer_portal_api.raise_inquiry(item=self.item, qty=1)

		frappe.set_user(self.other_dealer_user)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_inquiry(inquiry["id"])

	def test_my_dues_is_zero_with_no_invoices_raised_yet(self):
		self.assertEqual(dealer_portal_api.my_dues(), {"outstanding": 0.0})

	def test_my_profile_returns_this_dealers_own_customer_fields(self):
		profile = dealer_portal_api.my_profile()
		self.assertEqual(profile["id"], self.dealer)
		self.assertEqual(profile["name"], self.dealer)

	def test_suggest_alternatives_excludes_the_item_itself(self):
		alternatives = dealer_portal_api.suggest_alternatives(item=self.item)
		self.assertNotIn(self.item, [row["code"] for row in alternatives])

	def test_a_staff_only_session_cannot_use_the_dealer_portal(self):
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_catalog()
