"""MOM 07-Oct-2026 §3.1 -- "the system will display the 6-month average freight cost per
kg, broken down by transport type (retail truck / bulk container), as a real-time
reference to help users set a safe cost buffer. This is a guide, not a fixed value."

One row is one shipment's actual freight: when, truck or container, how many kg it
carried, what it cost. The average is computed from these (sales.freight_cost_api.
freight_reference) -- nothing else in this app records what freight actually cost
(Tally's invoices carry no freight cost per shipment, and the Transporter Vehicle list
is empty), so this is the one place it can come from: entered by staff as shipments
happen, or bulk-imported from Pacific's own history through Desk's Data Import.

Staff-only by design: dealers never see freight cost figures (MOM §3.2 -- internal
cost details are hidden from the dealer-facing quotation).
"""

import frappe
from frappe import _
from frappe.model.document import Document


class FreightCostEntry(Document):
	def validate(self):
		if (self.weight_kg or 0) <= 0:
			frappe.throw(_("Weight must be greater than zero."))
		if (self.freight_cost or 0) <= 0:
			frappe.throw(_("Freight cost must be greater than zero."))
