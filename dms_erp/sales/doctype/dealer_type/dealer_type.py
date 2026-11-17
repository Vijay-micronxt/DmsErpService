"""Dealer Type (BRD C.1.4 / C.4.3) -- Retail / Bulk / Project / Distributor and whatever else
the business adds. A type can pin its dealers' orders to one Order Channel (a bulk dealer's
small order is still bulk), or leave it empty so the quantity rule decides."""

import frappe
from frappe import _
from frappe.model.document import Document


class DealerType(Document):
	def validate(self):
		self.type_name = (self.type_name or "").strip()
		if self.forces_channel and not frappe.db.get_value("Order Channel", self.forces_channel, "enabled"):
			frappe.throw(_("{0} is disabled and can't be used by a dealer type.").format(self.forces_channel), frappe.ValidationError)
