import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.warehouse import bay_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay
from dms_erp.warehouse.setup import setup_warehouse


class TestBayApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_warehouse()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_create_bay_defaults_capacity_from_dimensions(self):
		bay = make_bay("TEST-A-01", dimensions="36x8")
		self.assertEqual(bay["capacityBoxes"], 900)
		self.assertEqual(bay["freeBoxes"], 900)
		self.assertEqual(bay["occupancyPct"], 0)

	def test_list_warehouse_groups_returns_the_two_physical_warehouses(self):
		groups = bay_api.list_warehouse_groups()
		names = {g["name"] for g in groups}
		self.assertIn("Pacific Main — Morbi", names)
		self.assertIn("Pacific Buffer — Wankaner", names)

		main = next(g for g in groups if g["name"] == "Pacific Main — Morbi")
		self.assertEqual(main["id"], frappe.db.get_value("Warehouse", {"warehouse_name": "Pacific Main — Morbi"}, "name"))

	def test_create_bay_grid_creates_sequential_codes(self):
		main_warehouse = next(g["id"] for g in bay_api.list_warehouse_groups() if g["name"] == "Pacific Main — Morbi")
		result = bay_api.create_bay_grid(
			prefix="GRID",
			count=3,
			start_at=1,
			bay_type="main",
			dimensions="32x6",
			parent_warehouse=main_warehouse,
			zone="Zone-G",
		)
		self.assertEqual(result["created"], 3)
		for code in ("GRID-01", "GRID-02", "GRID-03"):
			self.assertTrue(frappe.db.exists("Warehouse", {"custom_bay_code": code}))

	def test_update_bay_patches_status_and_categories(self):
		make_bay("TEST-A-02")
		updated = bay_api.update_bay("TEST-A-02", {"status": "reserved", "suitableCategories": ["Wall Tiles", "Floor Tiles"]})
		self.assertEqual(updated["status"], "reserved")
		self.assertEqual(updated["suitableCategories"], ["Wall Tiles", "Floor Tiles"])

	def test_write_requires_warehouse_or_management_role(self):
		make_bay("TEST-A-03")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			bay_api.update_bay("TEST-A-03", {"status": "blocked"})
