import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.purchase import po_api
from dms_erp.purchase.setup import setup_purchase
from dms_erp.warehouse.inward_api import add_truck
from dms_erp.warehouse.test_fixtures import ensure_company, make_item, make_supplier


class TestPoApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_purchase()
		cls.item = make_item("PO-TEST-ITEM", "Vitrified")
		cls.supplier = make_supplier("PO Test Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_purchase_order_is_submitted_with_one_line(self):
		po = po_api.create_purchase_order(
			item=self.item, ordered_qty=1000, supplier=self.supplier, expected_ready_date="2026-09-01", remarks="Launch batch"
		)
		self.assertEqual(len(po["lines"]), 1)
		self.assertEqual(po["lines"][0]["orderedQty"], 1000)
		self.assertEqual(po["lines"][0]["readyQty"], 0)
		self.assertEqual(frappe.db.get_value("Purchase Order", po["id"], "docstatus"), 1)

	def test_set_line_ready_clamps_to_ordered_qty(self):
		po = po_api.create_purchase_order(item=self.item, ordered_qty=500, supplier=self.supplier, expected_ready_date="2026-09-01")
		line_id = po["lines"][0]["id"]

		updated = po_api.set_line_ready(po["id"], line_id, 9999)
		self.assertEqual(updated["readyQty"], 500)

		updated = po_api.set_line_ready(po["id"], line_id, -10)
		self.assertEqual(updated["readyQty"], 0)

	def test_line_progress_reflects_linked_trucks(self):
		po = po_api.create_purchase_order(item=self.item, ordered_qty=1000, supplier=self.supplier, expected_ready_date="2026-09-01")
		line_id = po["lines"][0]["id"]

		progress = po_api.line_progress(line_id)
		self.assertEqual(progress["status"], "Awaiting readiness")

		po_api.set_line_ready(po["id"], line_id, 600)
		progress = po_api.line_progress(line_id)
		self.assertEqual(progress["status"], "Ready to plan")

		add_truck(supplier=self.supplier, item=self.item, boxes=300, purchase_order=po["id"], purchase_order_item=line_id)
		progress = po_api.line_progress(line_id)
		self.assertEqual(progress["plannedQty"], 300)
		self.assertEqual(progress["status"], "Partially planned")
		self.assertEqual(progress["remainingToPlan"], 300)

	def test_write_requires_purchase_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			po_api.create_purchase_order(item=self.item, ordered_qty=100, supplier=self.supplier, expected_ready_date="2026-09-01")

	def test_source_inquiry_is_optional_and_defaults_to_none(self):
		po = po_api.create_purchase_order(item=self.item, ordered_qty=100, supplier=self.supplier, expected_ready_date="2026-09-01")
		self.assertIsNone(po["sourceInquiry"])

	def test_list_purchase_orders_is_paginated(self):
		before = po_api.list_purchase_orders()
		baseline_total = before["total"]

		for _ in range(3):
			po_api.create_purchase_order(item=self.item, ordered_qty=100, supplier=self.supplier, expected_ready_date="2026-09-01")

		page = po_api.list_purchase_orders(limit=2, offset=0)
		self.assertEqual(page["total"], baseline_total + 3)
		self.assertEqual(len(page["items"]), 2)
		self.assertEqual(page["limit"], 2)
		self.assertEqual(page["offset"], 0)
