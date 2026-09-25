import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing.dealer_classification import (
	DEALER_CLASSIFICATION_DEALER,
	DEALER_CLASSIFICATION_MASTER,
	DEALER_CLASSIFICATION_STANDARD,
	recompute_dealer_classifications,
)
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item, make_supplier
from dms_erp.warehouse.utils import default_company


class TestDealerClassification(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		cls.supplier = make_supplier("Classification Test Supplier")
		cls.item = make_item("CLASSIFICATION-TEST-ITEM", "Vitrified")

	def _confirmed_order(self, dealer, grand_total):
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": dealer,
				"company": default_company(),
				"transaction_date": today(),
				"delivery_date": today(),
				"items": [{"item_code": self.item, "qty": 1, "rate": grand_total}],
			}
		)
		so.insert(ignore_permissions=True)
		so.submit()
		return so.name

	def test_new_dealer_defaults_to_standard(self):
		dealer = make_dealer("Classification New Dealer")
		self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_dealer_classification"), DEALER_CLASSIFICATION_STANDARD)

	def test_recompute_promotes_dealer_and_master_dealer_tiers(self):
		standard_dealer = make_dealer("Classification Standard Dealer")
		dealer_tier = make_dealer("Classification Dealer Tier")
		master_dealer = make_dealer("Classification Master Dealer")

		self._confirmed_order(standard_dealer, 50_000)
		self._confirmed_order(dealer_tier, 150_000)
		self._confirmed_order(master_dealer, 1_600_000)

		recompute_dealer_classifications()

		self.assertEqual(frappe.db.get_value("Customer", standard_dealer, "custom_dealer_classification"), DEALER_CLASSIFICATION_STANDARD)
		self.assertEqual(frappe.db.get_value("Customer", dealer_tier, "custom_dealer_classification"), DEALER_CLASSIFICATION_DEALER)
		self.assertEqual(frappe.db.get_value("Customer", master_dealer, "custom_dealer_classification"), DEALER_CLASSIFICATION_MASTER)

	def test_recompute_only_counts_submitted_orders_within_the_window(self):
		dealer = make_dealer("Classification Draft Only Dealer")
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": dealer,
				"company": default_company(),
				"transaction_date": today(),
				"delivery_date": today(),
				"items": [{"item_code": self.item, "qty": 1, "rate": 200_000}],
			}
		)
		so.insert(ignore_permissions=True)
		# Left as Draft (docstatus=0) -- unsubmitted value should not count.

		recompute_dealer_classifications()

		self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_dealer_classification"), DEALER_CLASSIFICATION_STANDARD)

	def test_recompute_floors_out_of_station_dealer_to_master(self):
		dealer = make_dealer("Classification Out Of Station Dealer")
		frappe.db.set_value("Customer", dealer, "custom_out_of_station", 1)
		self._confirmed_order(dealer, 50_000)  # would otherwise stay Standard Dealer

		recompute_dealer_classifications()

		self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_dealer_classification"), DEALER_CLASSIFICATION_MASTER)

	def test_recompute_never_lowers_a_high_volume_out_of_station_dealer(self):
		dealer = make_dealer("Classification Out Of Station High Volume Dealer")
		frappe.db.set_value("Customer", dealer, "custom_out_of_station", 1)
		self._confirmed_order(dealer, 1_600_000)  # already earns Master Dealer on volume alone

		recompute_dealer_classifications()

		self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_dealer_classification"), DEALER_CLASSIFICATION_MASTER)

	def test_recompute_returns_the_number_of_customers_changed(self):
		dealer = make_dealer("Classification Changed Count Dealer")
		self._confirmed_order(dealer, 200_000)

		changed = recompute_dealer_classifications()
		self.assertGreaterEqual(changed, 1)

		# Nothing left to change on a second run -- band stays Dealer either way.
		changed_again = recompute_dealer_classifications()
		self.assertEqual(changed_again, 0)
