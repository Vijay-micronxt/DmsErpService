import frappe
from frappe.model.document import Document


class DMSCatalogSettings(Document):
	def on_update(self):
		# status_label() keeps the label table for the rest of the request; a change made in
		# this request (a test, a script) must be seen straight away.
		frappe.local.dms_status_labels = None
