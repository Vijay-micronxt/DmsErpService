import frappe
from frappe import _
from frappe.model.document import Document


class DealerPurchaseOrder(Document):
	def validate(self):
		"""A PO is one dealer's. Whatever it is linked to must be that same dealer's document -- a PO
		cannot be hung on someone else's inquiry, quotation or order, by Desk or by the API."""
		for field, doctype, dealer_field in (
			("inquiry", "Inquiry", "dealer"),
			("quotation", "Quotation", "party_name"),
			("sales_order", "Sales Order", "customer"),
		):
			linked = self.get(field)
			if linked and frappe.db.get_value(doctype, linked, dealer_field) != self.dealer:
				frappe.throw(_("{0} {1} does not belong to {2}.").format(doctype, linked, self.dealer), frappe.ValidationError)
