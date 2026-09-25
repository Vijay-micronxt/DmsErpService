import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog import series_api
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

	def test_purchase_order_line_carries_the_items_weight(self):
		frappe.db.set_value("Item", self.item, "custom_weight_per_box_kg", 28)
		po = po_api.create_purchase_order(item=self.item, ordered_qty=1000, supplier=self.supplier, expected_ready_date="2026-09-01")
		self.assertEqual(po["lines"][0]["weightPerBoxKg"], 28)
		self.assertEqual(po["lines"][0]["totalWeightKg"], 28000)

	def test_purchase_order_line_carries_the_items_pieces_and_sqft(self):
		frappe.db.set_value("Item", self.item, "custom_pieces_per_box", 4)
		frappe.db.set_value("Item", self.item, "custom_sqft_per_box", 15.5)
		po = po_api.create_purchase_order(item=self.item, ordered_qty=1000, supplier=self.supplier, expected_ready_date="2026-09-01")
		self.assertEqual(po["lines"][0]["piecesPerBox"], 4)
		self.assertEqual(po["lines"][0]["totalPieces"], 4000)
		self.assertEqual(po["lines"][0]["sqftPerBox"], 15.5)
		self.assertEqual(po["lines"][0]["totalSqft"], 15500)
		self.assertAlmostEqual(po["lines"][0]["sqmPerBox"], 1.44, places=2)
		self.assertAlmostEqual(po["lines"][0]["totalSqm"], 1440, places=2)

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

	def test_create_purchase_order_falls_back_to_the_items_default_supplier(self):
		item = make_item("PO-DEFAULT-SUPPLIER-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_default_supplier", self.supplier)

		po = po_api.create_purchase_order(item=item, ordered_qty=100, expected_ready_date="2026-09-01")
		self.assertEqual(po["supplier"], self.supplier)

	def test_create_purchase_order_falls_back_to_the_series_supplier(self):
		item = make_item("PO-SERIES-SUPPLIER-ITEM", "Vitrified")
		series = series_api.create_series(series_name="PO Test Series", supplier=self.supplier)
		frappe.db.set_value("Item", item, "custom_series_ref", series["id"])

		po = po_api.create_purchase_order(item=item, ordered_qty=100, expected_ready_date="2026-09-01")
		self.assertEqual(po["supplier"], self.supplier)

	def test_create_purchase_order_explicit_supplier_wins_over_the_default(self):
		item = make_item("PO-EXPLICIT-SUPPLIER-ITEM", "Vitrified")
		frappe.db.set_value("Item", item, "custom_default_supplier", self.supplier)
		other_supplier = make_supplier("PO Other Explicit Supplier")

		po = po_api.create_purchase_order(item=item, ordered_qty=100, supplier=other_supplier, expected_ready_date="2026-09-01")
		self.assertEqual(po["supplier"], other_supplier)

	def test_create_purchase_order_requires_a_supplier_when_none_can_be_resolved(self):
		item = make_item("PO-NO-SUPPLIER-ITEM", "Vitrified")
		with self.assertRaises(frappe.ValidationError):
			po_api.create_purchase_order(item=item, ordered_qty=100, expected_ready_date="2026-09-01")

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
