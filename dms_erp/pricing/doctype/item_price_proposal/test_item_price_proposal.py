import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing


def make_item(item_code):
	if frappe.db.exists("Item", item_code):
		frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"is_stock_item": 1,
		}
	).insert(ignore_permissions=True)


def make_supplier(supplier_name):
	if frappe.db.exists("Supplier", supplier_name):
		return supplier_name
	frappe.get_doc(
		{"doctype": "Supplier", "supplier_name": supplier_name, "supplier_group": "All Supplier Groups"}
	).insert(ignore_permissions=True)
	return supplier_name


class TestItemPriceProposal(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup_pricing()
		cls.item = "PRICE-TEST-ITEM"
		make_item(cls.item)
		cls.supplier = make_supplier("Pricing Test Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("Item Price Proposal", self.item):
			frappe.delete_doc("Item Price Proposal", self.item, force=True, ignore_permissions=True)

	def test_ensure_price_record_creates_pending_once(self):
		pricing_api.ensure_price_record(self.item, self.supplier, 100, 20, "2026-08-01")
		record = pricing_api.get_price_record(self.item)
		self.assertEqual(record["status"], "Pending")
		self.assertEqual(record["landingCost"], 100)
		self.assertEqual(record["suggestedPrice"], 120)

		# calling again must not duplicate / reset it
		pricing_api.ensure_price_record(self.item, self.supplier, 999, 99, "2026-01-01")
		self.assertEqual(pricing_api.get_price_record(self.item)["purchaseCost"], 100)

	def test_save_cost_inputs_requires_purchase_or_management_role(self):
		pricing_api.ensure_price_record(self.item, self.supplier, 100, 20, "2026-08-01")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			pricing_api.save_cost_inputs(
				item=self.item, supplier=self.supplier, purchase_cost=110, margin_pct=22, effective_date="2026-08-05"
			)

	def test_approve_price_publishes_item_price_and_history(self):
		pricing_api.ensure_price_record(self.item, self.supplier, 100, 20, "2026-08-01")
		frappe.set_user("Administrator")
		result = pricing_api.approve_price(item=self.item, final_price=150, reason="Launch price")

		self.assertEqual(result["status"], "Approved")
		self.assertEqual(len(result["history"]), 1)
		self.assertEqual(result["history"][0]["newPrice"], 150)
		self.assertEqual(result["history"][0]["approvedBy"], "Administrator")
		self.assertEqual(pricing_api.get_dealer_price(self.item), 150)

		# re-approving at a new price should append, not replace, history
		result2 = pricing_api.approve_price(item=self.item, final_price=160, reason="Cost increase")
		self.assertEqual(len(result2["history"]), 2)
		self.assertEqual(result2["history"][0]["oldPrice"], 150)
		self.assertEqual(pricing_api.get_dealer_price(self.item), 160)
