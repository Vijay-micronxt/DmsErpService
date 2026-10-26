import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.catalog.utils import (
	DISCONTINUATION_STATUSES,
	convert_qty_to_boxes,
	derive_sqft_per_box,
	normalize_order_line_uoms,
	status_label,
)
from dms_erp.warehouse.test_fixtures import ensure_company, make_item


class TestStatusLabel(FrappeTestCase):
	def tearDown(self):
		settings = frappe.get_single("DMS Catalog Settings")
		settings.status_labels = []
		settings.save(ignore_permissions=True)

	def test_unconfigured_status_falls_back_to_its_own_raw_value(self):
		"""A fresh site (no DMS Catalog Settings rows at all) must be a no-op for
		every real status -- never a blank label, never a fabricated default."""
		for status in DISCONTINUATION_STATUSES:
			self.assertEqual(status_label(status), status)

	def test_an_unknown_status_also_falls_back_to_itself(self):
		self.assertEqual(status_label("Some Future Status"), "Some Future Status")

	def test_configured_label_overrides_the_raw_value(self):
		settings = frappe.get_single("DMS Catalog Settings")
		settings.append("status_labels", {"status": "Partially Discontinued", "label": "PDC"})
		settings.append("status_labels", {"status": "Factory Discontinued", "label": "Against Order"})
		settings.save(ignore_permissions=True)

		self.assertEqual(status_label("Partially Discontinued"), "PDC")
		self.assertEqual(status_label("Factory Discontinued"), "Against Order")
		# Every other status stays unconfigured -- not dragged along by the two rows above.
		self.assertEqual(status_label("Active"), "Active")
		self.assertEqual(status_label("Display Removal Pending"), "Display Removal Pending")
		self.assertEqual(status_label("Pulled Back"), "Pulled Back")

	def test_a_blank_configured_label_also_falls_back_to_the_raw_value(self):
		"""A row that exists but whose label was left blank (not deleted) must
		behave identically to no row at all -- `or status`, not a cached empty
		string, is what makes this safe."""
		settings = frappe.get_single("DMS Catalog Settings")
		settings.append("status_labels", {"status": "Pulled Back", "label": ""})
		settings.save(ignore_permissions=True)

		self.assertEqual(status_label("Pulled Back"), "Pulled Back")


class TestDeriveSqftPerBox(FrappeTestCase):
	def test_derives_from_height_width_and_pieces(self):
		# A 600mm x 600mm tile, 4 pieces per box: (600/304.8)^2 * 4 = 15.5 sqft.
		self.assertEqual(derive_sqft_per_box(600, 600, 4), 15.5)

	def test_missing_any_input_returns_none(self):
		self.assertIsNone(derive_sqft_per_box(None, 600, 4))
		self.assertIsNone(derive_sqft_per_box(600, None, 4))
		self.assertIsNone(derive_sqft_per_box(600, 600, None))
		self.assertIsNone(derive_sqft_per_box(0, 600, 4))


class TestConvertQtyToBoxes(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		cls.item = make_item("UOM-TEST-ITEM", "Vitrified")
		frappe.db.set_value(
			"Item",
			cls.item,
			{
				"custom_pieces_per_box": 4,
				"custom_sqft_per_box": 15.5,
				"custom_weight_per_box_kg": 28,
			},
		)

	def test_box_passes_through_unchanged(self):
		self.assertEqual(convert_qty_to_boxes(self.item, 7, "Box"), 7)

	def test_pcs_converts_and_rounds_up(self):
		# 4 pieces/box -> 10 pcs needs 3 boxes (2.5 rounded up).
		self.assertEqual(convert_qty_to_boxes(self.item, 10, "Pcs"), 3)

	def test_sqft_converts_and_rounds_up(self):
		self.assertEqual(convert_qty_to_boxes(self.item, 50, "Sqft"), 4)  # 50 / 15.5 = 3.22 -> 4

	def test_sqm_converts_and_rounds_up(self):
		# sqm_per_box derives from sqft_per_box (15.5 * 0.09290304 ~= 1.44sqm).
		self.assertEqual(convert_qty_to_boxes(self.item, 4, "Sqm"), 3)  # 4 / 1.44 = 2.78 -> 3

	def test_kg_converts_and_rounds_up(self):
		self.assertEqual(convert_qty_to_boxes(self.item, 50, "Kg"), 2)  # 50 / 28 = 1.79 -> 2

	def test_gram_converts_and_rounds_up(self):
		self.assertEqual(convert_qty_to_boxes(self.item, 50000, "Gram"), 2)  # 50000 / 28000 -> 2

	def test_unknown_uom_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			convert_qty_to_boxes(self.item, 10, "Litre")

	def test_missing_conversion_factor_is_rejected(self):
		no_factors = make_item("UOM-TEST-NO-FACTORS", "Vitrified")
		with self.assertRaises(frappe.ValidationError):
			convert_qty_to_boxes(no_factors, 10, "Sqft")

	def test_normalize_order_line_uoms_converts_and_tags_non_box_lines(self):
		lines = [
			{"item": self.item, "qty": 10},
			{"item": self.item, "qty": 50, "uom": "Sqft"},
		]
		out = normalize_order_line_uoms(lines)
		self.assertEqual(out[0]["qty"], 10)
		self.assertNotIn("entered_qty", out[0])
		self.assertNotIn("entered_uom", out[0])
		self.assertEqual(out[1]["qty"], 4)
		self.assertEqual(out[1]["entered_qty"], 50)
		self.assertEqual(out[1]["entered_uom"], "Sqft")
