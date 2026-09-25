import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.warehouse import inward_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_item, make_supplier


class TestInward(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		cls.item = make_item("INWARD-TEST-ITEM", "Vitrified")
		cls.supplier = make_supplier("Inward Test Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_add_truck_defaults_to_scheduled(self):
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=500, lr_number="LR-TEST-1")
		self.assertEqual(truck["status"], "Scheduled")
		self.assertEqual(truck["boxes"], 500)

	def test_truck_weight_falls_back_to_the_items_standard_weight_before_allocation(self):
		frappe.db.set_value("Item", self.item, "custom_weight_per_box_kg", 28)
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=50)
		self.assertEqual(truck["weightPerBoxKg"], 28)
		self.assertEqual(truck["totalWeightKg"], 1400)

	def test_truck_carries_the_items_pieces_and_sqft(self):
		frappe.db.set_value("Item", self.item, "custom_pieces_per_box", 4)
		frappe.db.set_value("Item", self.item, "custom_sqft_per_box", 15.5)
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=50)
		self.assertEqual(truck["piecesPerBox"], 4)
		self.assertEqual(truck["totalPieces"], 200)
		self.assertEqual(truck["sqftPerBox"], 15.5)
		self.assertEqual(truck["totalSqft"], 775)

	def test_truck_weight_prefers_the_confirmed_batchs_own_weight_once_allocated(self):
		frappe.db.set_value("Item", self.item, "custom_weight_per_box_kg", 28)
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=50)
		frappe.get_doc({"doctype": "Batch", "batch_id": "INWARD-WEIGHT-BATCH", "item": self.item, "custom_batch_weight_kg": 31.5}).insert(
			ignore_permissions=True
		)
		frappe.db.set_value("Inward Truck", truck["id"], "batch_no", "INWARD-WEIGHT-BATCH")

		refetched = inward_api.list_trucks(search=truck["lr"])["items"][0]
		self.assertEqual(refetched["weightPerBoxKg"], 31.5)

	def test_advance_truck_follows_flow(self):
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=200)
		for status in ("At Gate", "Unloading", "Put-away"):
			updated = inward_api.advance_truck(truck["id"], status)
			self.assertEqual(updated["status"], status)

	def test_advance_truck_rejects_invalid_status(self):
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=200)
		with self.assertRaises(frappe.ValidationError):
			inward_api.advance_truck(truck["id"], "Delivered")

	def test_add_truck_accepts_a_full_eta_string(self):
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=50, eta="2026-09-05 10:00:00")
		self.assertIsNotNone(truck["eta"])

	def test_add_truck_leniently_parses_a_time_only_eta(self):
		# Real production input: a caller sent bare "10:00" for a Datetime field. That
		# previously reached MariaDB raw and failed with an unhandled OperationalError
		# ("Incorrect datetime value"). get_datetime() parses this leniently (dateutil
		# fills in today's date) rather than rejecting it -- better UX than an error for
		# a value this unambiguous, and it's the same parser Frappe uses everywhere else
		# a user types a date/time by hand.
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=50, eta="10:00")
		self.assertIsNotNone(truck["eta"])

	def test_add_truck_rejects_an_unparseable_eta(self):
		# Real production input: a caller sent "Tomorrow 10:00" -- a relative expression
		# no date/time parser resolves -- which previously reached MariaDB raw and failed
		# with an unhandled OperationalError instead of a clean validation message.
		with self.assertRaises(frappe.ValidationError):
			inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=50, eta="Tomorrow 10:00")

	def test_write_requires_warehouse_purchase_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=1)

	def test_list_trucks_is_paginated(self):
		before = inward_api.list_trucks()
		baseline_total = before["total"]

		for i in range(3):
			inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=100, lr_number=f"LR-PAGE-{i}")

		page = inward_api.list_trucks(limit=2, offset=0)
		self.assertEqual(page["total"], baseline_total + 3)
		self.assertEqual(len(page["items"]), 2)
		self.assertEqual(page["limit"], 2)
		self.assertEqual(page["offset"], 0)
