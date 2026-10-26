import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.sales import invoice_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_item
from dms_erp.warehouse.utils import ensure_batch


class TestInvoiceWeight(FrappeTestCase):
	"""BRD C.1.3 -- weight on the invoice screen. Built on an unsaved Sales Invoice: the
	weight rule only reads the lines, and posting a real invoice needs accounting setup
	that has nothing to do with what's being tested here."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		cls.heavy = make_item("INV-WEIGHT-HEAVY", "Vitrified")
		frappe.db.set_value("Item", cls.heavy, "custom_weight_per_box_kg", 25)
		cls.unweighed = make_item("INV-WEIGHT-NONE", "Vitrified")
		frappe.db.set_value("Item", cls.unweighed, "custom_weight_per_box_kg", 0)
		frappe.clear_cache(doctype="Item")

	def _invoice(self, *rows):
		return frappe.get_doc({"doctype": "Sales Invoice", "items": [dict(r) for r in rows]})

	def test_line_weight_is_stock_qty_times_weight_per_box(self):
		doc = self._invoice({"item_code": self.heavy, "qty": 40, "stock_qty": 40})
		detail = invoice_api._invoice_detail(doc)
		self.assertEqual(detail["items"][0]["weightKg"], 1000)
		self.assertEqual(detail["totalWeightKg"], 1000)

	def test_uses_stock_qty_when_billed_in_another_uom(self):
		# 90 pieces billed on a 9-pcs box is 10 boxes in stock UOM.
		doc = self._invoice({"item_code": self.heavy, "qty": 90, "uom": "Nos", "stock_qty": 10})
		self.assertEqual(invoice_api._invoice_detail(doc)["items"][0]["weightKg"], 250)

	def test_batch_weight_wins_over_the_items_standard_weight(self):
		ensure_batch(self.heavy, "INV-WEIGHT-B1", weight_per_box_kg=27.5)
		doc = self._invoice({"item_code": self.heavy, "qty": 10, "stock_qty": 10, "batch_no": "INV-WEIGHT-B1"})
		self.assertEqual(invoice_api._invoice_detail(doc)["items"][0]["weightKg"], 275)

	def test_total_is_withheld_when_any_line_has_no_weight(self):
		doc = self._invoice(
			{"item_code": self.heavy, "qty": 10, "stock_qty": 10},
			{"item_code": self.unweighed, "qty": 5, "stock_qty": 5},
		)
		detail = invoice_api._invoice_detail(doc)
		self.assertEqual(detail["items"][0]["weightKg"], 250)
		self.assertIsNone(detail["items"][1]["weightKg"])
		self.assertIsNone(detail["totalWeightKg"])
