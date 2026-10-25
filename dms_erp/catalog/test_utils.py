import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.utils import DISCONTINUATION_STATUSES, status_label


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
