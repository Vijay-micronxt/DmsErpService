import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.sales.order_channel import auto_classify_channel
from dms_erp.sales.setup import setup_sales
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item


class TestOrderChannel(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_sales()
		cls.item = make_item("CHANNEL-TEST-ITEM", "Vitrified")

	def test_untagged_dealer_and_item_default_to_retail(self):
		dealer = make_dealer("Channel Untagged Dealer")
		self.assertEqual(auto_classify_channel(dealer, [{"item": self.item, "qty": 1000}]), "Retail")

	def test_bulk_dealer_type_always_wins(self):
		dealer = make_dealer("Channel Bulk Dealer")
		frappe.db.set_value("Customer", dealer, "custom_dealer_type", "Bulk")
		self.assertEqual(auto_classify_channel(dealer, [{"item": self.item, "qty": 1}]), "Bulk")

	def test_project_dealer_type_always_wins(self):
		dealer = make_dealer("Channel Project Dealer")
		frappe.db.set_value("Customer", dealer, "custom_dealer_type", "Project")
		self.assertEqual(auto_classify_channel(dealer, [{"item": self.item, "qty": 1}]), "Project")

	def test_qty_at_or_above_the_items_bulk_threshold_classifies_bulk(self):
		dealer = make_dealer("Channel Threshold Dealer")
		frappe.db.set_value("Item", self.item, "custom_bulk_qty_threshold", 200)
		self.assertEqual(auto_classify_channel(dealer, [{"item": self.item, "qty": 200}]), "Bulk")
		self.assertEqual(auto_classify_channel(dealer, [{"item": self.item, "qty": 199}]), "Retail")

	def test_item_with_no_threshold_never_triggers_bulk_on_qty_alone(self):
		dealer = make_dealer("Channel No Threshold Dealer")
		frappe.db.set_value("Item", self.item, "custom_bulk_qty_threshold", 0)
		self.assertEqual(auto_classify_channel(dealer, [{"item": self.item, "qty": 999_999}]), "Retail")
