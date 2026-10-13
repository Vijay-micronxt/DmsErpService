import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.sales.utils import safe_customer_message


class TestSafeCustomerMessage(FrappeTestCase):
	def test_passes_through_our_own_plain_text_messages(self):
		msg = safe_customer_message(frappe.ValidationError("GVT-6013 has no approved dealer price yet."))
		self.assertIn("GVT-6013 has no approved dealer price yet.", msg)
		self.assertIn("couldn't place this order", msg)
		self.assertIn("Pacific representative", msg)

	def test_sanitizes_erpnext_native_html_messages(self):
		# The real ERPNext Sales Order credit-limit-exceeded message -- names
		# internal Credit Controller users and their emails, meant for a Desk
		# user, never a dealer. The useful part (the credit-limit figures) is
		# dealer-safe and should survive; the internal contact list must not.
		native = (
			"Credit limit has been crossed for customer Medha - 1(37290.0/1000.0)<br><br>"
			"Please contact any of the following users to extend the credit limits for Medha - 1: "
			"<br><br> <ul><li>Karthik Mxt (karthikeyan@micronxt.com)</li></ul>."
		)
		msg = safe_customer_message(frappe.ValidationError(native))
		self.assertNotIn("<", msg)
		self.assertNotIn("micronxt.com", msg)
		self.assertNotIn("Karthik", msg)
		self.assertIn("Credit limit has been crossed for customer Medha - 1", msg)
		self.assertIn("couldn't place this order", msg)
		self.assertIn("Pacific representative", msg)

	def test_falls_back_to_generic_message_when_nothing_useful_survives(self):
		native = "<div>Please contact any of the following users: <a>a@b.com</a></div>"
		msg = safe_customer_message(frappe.ValidationError(native))
		self.assertNotIn("<", msg)
		self.assertNotIn("@", msg)
		self.assertIn("couldn't place this order right now", msg)
		self.assertIn("Pacific representative", msg)
