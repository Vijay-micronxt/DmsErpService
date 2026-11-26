import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.warehouse import bay_api
from dms_erp.warehouse.test_fixtures import bay_size_kwargs, ensure_company, make_bay
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.utils import bay_size_label, compute_bay_capacity


class TestBayApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_warehouse()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_compute_bay_capacity_matches_the_agreed_worked_example(self):
		# 40 x 6 ft bay, 600 mm tile, 1 inch box, 100 mm slot gap, 4 back / 5 front margin.
		result = compute_bay_capacity(12192, 1828.8, 100, 4, 5, tile_width_mm=600, box_thickness_mm=25.4)
		self.assertEqual(result["slots"], 17)  # 17 x 600 + 16 x 100 = 11,800 <= 12,192; 18 would need 12,500
		self.assertEqual(result["lowerStackBoxes"], 72)
		self.assertEqual(result["upperStackBoxes"], 63)
		self.assertEqual(result["boxesPerSlot"], 135)
		self.assertEqual(result["capacityBoxes"], 2295)

	def test_compute_bay_capacity_other_tile_widths_and_a_thicker_box(self):
		def slots(tile):
			return compute_bay_capacity(12192, 1828.8, 100, 4, 5, tile_width_mm=tile)["slots"]

		self.assertEqual((slots(300), slots(600), slots(1200)), (30, 17, 9))
		two_inch = compute_bay_capacity(12192, 1828.8, 100, 4, 5, box_thickness_mm=50.8)
		self.assertEqual((two_inch["lowerStackBoxes"], two_inch["upperStackBoxes"]), (36, 27))
		self.assertEqual(two_inch["capacityBoxes"], 17 * 63)

	def test_compute_bay_capacity_never_goes_negative(self):
		# Margins bigger than the stack leave no upper stack, not a negative one.
		result = compute_bay_capacity(12192, 100, 100, 4, 5)
		self.assertEqual(result["lowerStackBoxes"], 3)
		self.assertEqual(result["upperStackBoxes"], 0)
		# A bay too short for even one slot has zero capacity.
		self.assertEqual(compute_bay_capacity(300, 1828.8, 100, 4, 5)["capacityBoxes"], 0)

	def test_bay_size_label_is_derived_in_feet(self):
		self.assertEqual(bay_size_label(12192, 1828.8), "40x6")

	def test_create_bay_computes_capacity_and_label_from_the_sizes(self):
		bay = make_bay("TEST-A-01", dimensions="40x6")
		self.assertEqual(bay["capacityBoxes"], 2295)
		self.assertEqual(bay["freeBoxes"], 2295)
		self.assertEqual(bay["occupancyPct"], 0)
		self.assertEqual(bay["dimensions"], "40x6")
		# Millimetres come from feet x 304.8, so allow for float noise in the last digit.
		self.assertAlmostEqual(bay["lengthMm"], 12192, places=2)
		self.assertAlmostEqual(bay["widthMm"], 1828.8, places=2)
		self.assertEqual((bay["slotGapMm"], bay["backMarginBoxes"], bay["frontMarginBoxes"], bay["topClearanceMm"]), (100, 4, 5, 15))

	def test_create_bay_requires_every_size_field(self):
		main_warehouse = next(g["id"] for g in bay_api.list_warehouse_groups() if g["name"] == "Pacific Main — Morbi")
		sizes = bay_size_kwargs("40x6")
		for field in sizes:
			with self.subTest(missing=field), self.assertRaises(frappe.ValidationError):
				bay_api.create_bay(
					code="REQ-A-01", bay_type="main", parent_warehouse=main_warehouse, zone="Z", row="R1",
					**{**sizes, field: None},
				)
		self.assertFalse(frappe.db.exists("Warehouse", {"custom_bay_code": "REQ-A-01"}))

	def test_create_bay_rejects_nonsense_sizes(self):
		main_warehouse = next(g["id"] for g in bay_api.list_warehouse_groups() if g["name"] == "Pacific Main — Morbi")
		for bad in ({"length_mm": 0}, {"width_mm": -5}, {"slot_gap_mm": -1}, {"top_clearance_mm": 99999}):
			with self.subTest(bad=bad), self.assertRaises(frappe.ValidationError):
				bay_api.create_bay(
					code="BAD-A-01", bay_type="main", parent_warehouse=main_warehouse, zone="Z", row="R1",
					**{**bay_size_kwargs("40x6"), **bad},
				)

	def test_update_bay_recomputes_capacity_when_a_size_changes(self):
		make_bay("TEST-A-04", dimensions="40x6")
		# Halving the length: (6096 + 100) // 700 = 8 slots -> 8 x 135.
		updated = bay_api.update_bay("TEST-A-04", {"lengthMm": 6096})
		self.assertEqual(updated["capacityBoxes"], 8 * 135)
		self.assertEqual(updated["dimensions"], "20x6")

	def test_update_bay_ignores_a_hand_typed_capacity(self):
		make_bay("TEST-A-05", dimensions="40x6")
		updated = bay_api.update_bay("TEST-A-05", {"capacityBoxes": 5, "dimensions": "1x1"})
		self.assertEqual(updated["capacityBoxes"], 2295)
		self.assertEqual(updated["dimensions"], "40x6")

	def test_list_bay_size_options_offers_the_defaults_and_every_saved_value(self):
		make_bay("TEST-A-06", dimensions="40x6")
		bay_api.update_bay("TEST-A-06", {"slotGapMm": 120, "backMarginBoxes": 3})

		options = bay_api.list_bay_size_options()

		self.assertIn(100, options["slotGapMm"])  # starting default stays on offer
		self.assertIn(120, options["slotGapMm"])  # a value saved on a bay is offered on the next
		self.assertIn(3, options["backMarginBoxes"])
		self.assertIn(4, options["backMarginBoxes"])
		self.assertIn(5, options["frontMarginBoxes"])
		self.assertIn(15, options["topClearanceMm"])
		self.assertIn(12192, options["lengthMm"])

	def test_list_warehouse_groups_returns_the_two_physical_warehouses(self):
		groups = bay_api.list_warehouse_groups()
		names = {g["name"] for g in groups}
		self.assertIn("Pacific Main — Morbi", names)
		self.assertIn("Pacific Buffer — Wankaner", names)

		main = next(g for g in groups if g["name"] == "Pacific Main — Morbi")
		self.assertEqual(main["id"], frappe.db.get_value("Warehouse", {"warehouse_name": "Pacific Main — Morbi"}, "name"))

	def test_create_warehouse_group_adds_a_third_physical_site(self):
		created = bay_api.create_warehouse_group("Pacific Overflow — Rajkot")
		self.assertEqual(created["name"], "Pacific Overflow — Rajkot")

		groups = bay_api.list_warehouse_groups()
		self.assertIn("Pacific Overflow — Rajkot", {g["name"] for g in groups})

		bay = bay_api.create_bay(
			code="OVERFLOW-A-01",
			bay_type="main",
			parent_warehouse=created["id"],
			zone="Zone-O",
			row="R1",
			**bay_size_kwargs("32x6"),
		)
		self.assertEqual(bay["warehouse"], "Pacific Overflow — Rajkot")

	def test_create_warehouse_group_rejects_a_duplicate_name(self):
		with self.assertRaises(frappe.DuplicateEntryError):
			bay_api.create_warehouse_group("Pacific Main — Morbi")

	def test_list_warehouse_groups_search_filters_by_name(self):
		bay_api.create_warehouse_group("Pacific Search Test — Junagadh")

		found = bay_api.list_warehouse_groups(search="Search Test")
		self.assertEqual({g["name"] for g in found}, {"Pacific Search Test — Junagadh"})

		self.assertNotIn("Pacific Search Test — Junagadh", {g["name"] for g in bay_api.list_warehouse_groups(search="Morbi")})

	def test_create_bay_grid_creates_sequential_codes(self):
		main_warehouse = next(g["id"] for g in bay_api.list_warehouse_groups() if g["name"] == "Pacific Main — Morbi")
		result = bay_api.create_bay_grid(
			prefix="GRID",
			count=3,
			start_at=1,
			bay_type="main",
			parent_warehouse=main_warehouse,
			zone="Zone-G",
			**bay_size_kwargs("32x6"),
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

	def test_list_bays_is_paginated_and_searchable(self):
		for code in ("PAGE-B-01", "PAGE-B-02", "PAGE-B-03"):
			make_bay(code)

		page = bay_api.list_bays(search="PAGE-B-", limit=2, offset=0)
		self.assertEqual(page["total"], 3)
		self.assertEqual(len(page["items"]), 2)
		self.assertEqual(page["limit"], 2)
		self.assertEqual(page["offset"], 0)

		next_page = bay_api.list_bays(search="PAGE-B-", limit=2, offset=2)
		self.assertEqual(len(next_page["items"]), 1)

		found = bay_api.list_bays(search="PAGE-B-02")
		self.assertEqual(found["total"], 1)
		self.assertEqual(found["items"][0]["code"], "PAGE-B-02")

	def test_list_all_bays_returns_the_full_unpaginated_set(self):
		for code in ("ALL-B-01", "ALL-B-02", "ALL-B-03"):
			make_bay(code)

		codes = {b["code"] for b in bay_api.list_all_bays(search="ALL-B-")}
		self.assertEqual(codes, {"ALL-B-01", "ALL-B-02", "ALL-B-03"})

	def test_create_bay_accepts_a_linked_main_bay(self):
		main = make_bay("LINK-MAIN-01", bay_type="main")
		buffer_bay = bay_api.create_bay(
			code="LINK-BUF-01",
			bay_type="buffer",
			parent_warehouse=frappe.db.get_value("Warehouse", main["id"], "parent_warehouse"),
			zone="Zone-A",
			row="R1",
			linked_main_bay=main["id"],
			**bay_size_kwargs("36x8"),
		)
		self.assertEqual(buffer_bay["linkedMainBay"], main["id"])

	def test_create_bay_rejects_a_linked_main_bay_that_is_not_type_main(self):
		buffer_target = make_bay("LINK-NOTMAIN-01", bay_type="buffer")
		with self.assertRaises(frappe.ValidationError):
			bay_api.create_bay(
				code="LINK-BUF-02",
				bay_type="buffer",
				parent_warehouse=frappe.db.get_value("Warehouse", buffer_target["id"], "parent_warehouse"),
				zone="Zone-A",
				row="R1",
				linked_main_bay=buffer_target["id"],
				**bay_size_kwargs("36x8"),
			)

	def test_update_bay_sets_and_rejects_linked_main_bay(self):
		main = make_bay("LINK-MAIN-02", bay_type="main")
		buffer_bay = make_bay("LINK-BUF-03", bay_type="buffer")

		updated = bay_api.update_bay("LINK-BUF-03", {"linkedMainBay": main["id"]})
		self.assertEqual(updated["linkedMainBay"], main["id"])

		with self.assertRaises(frappe.ValidationError):
			bay_api.update_bay("LINK-BUF-03", {"linkedMainBay": buffer_bay["id"]})

	def test_list_bays_filters_by_type_status_and_parent_warehouse(self):
		main_warehouse = next(g["id"] for g in bay_api.list_warehouse_groups() if g["name"] == "Pacific Main — Morbi")
		buffer_warehouse = next(g["id"] for g in bay_api.list_warehouse_groups() if g["name"] == "Pacific Buffer — Wankaner")

		make_bay("FILTER-MAIN-01", bay_type="main", parent_warehouse=main_warehouse)
		make_bay("FILTER-BUFFER-01", bay_type="buffer", parent_warehouse=buffer_warehouse)
		bay_api.update_bay("FILTER-MAIN-01", {"status": "reserved"})

		by_type = bay_api.list_bays(bay_type="buffer", search="FILTER-")
		self.assertEqual({b["code"] for b in by_type["items"]}, {"FILTER-BUFFER-01"})

		by_status = bay_api.list_bays(status="reserved", search="FILTER-")
		self.assertEqual({b["code"] for b in by_status["items"]}, {"FILTER-MAIN-01"})

		by_parent = bay_api.list_bays(parent_warehouse=buffer_warehouse, search="FILTER-")
		self.assertEqual({b["code"] for b in by_parent["items"]}, {"FILTER-BUFFER-01"})
