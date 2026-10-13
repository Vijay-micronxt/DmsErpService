import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.sales.utils import dealer_contact_line, safe_customer_message
from dms_erp.warehouse.test_fixtures import make_dealer


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


def _make_salesperson(email: str, full_name: str, mobile: str | None = None) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": full_name, "mobile_no": mobile, "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	return email


class TestDealerContactLine(FrappeTestCase):
	"""WhatsApp Inquiry gap #5 -- the dealer's own assigned salesperson, listed, in
	place of the generic "contact your Pacific representative"."""

	def test_falls_back_to_generic_line_with_no_dealer_or_no_salesperson(self):
		self.assertIn("Pacific representative", dealer_contact_line(None))
		dealer = make_dealer("Contact Line No Salesperson Dealer")
		frappe.db.set_value("Customer", dealer, "custom_salesperson", None)
		self.assertIn("Pacific representative", dealer_contact_line(dealer))

	def test_lists_the_assigned_salespersons_details(self):
		dealer = make_dealer("Contact Line Dealer")
		rep = _make_salesperson("contact.line.rep@example.com", "Ravi", "9876543210")
		frappe.db.set_value("Customer", dealer, "custom_salesperson", rep)

		line = dealer_contact_line(dealer)

		self.assertIn("Name: Ravi", line)
		self.assertIn("Phone: 9876543210", line)
		self.assertIn("Email: contact.line.rep@example.com", line)
		self.assertNotIn("Pacific representative", line)

	def test_safe_customer_message_appends_the_salesperson_after_stripping_emails(self):
		# The email-stripping in safe_customer_message targets *internal* staff
		# leaked by ERPNext's own error text -- it must not also strip the dealer's
		# own salesperson's email, which is appended afterwards on purpose.
		dealer = make_dealer("Contact Line Order Error Dealer")
		rep = _make_salesperson("order.error.rep@example.com", "Meena")
		frappe.db.set_value("Customer", dealer, "custom_salesperson", rep)
		native = (
			"Credit limit has been crossed for customer X (10/5)<br>"
			"Please contact any of the following users: <li>Internal (internal@micronxt.com)</li>"
		)

		msg = safe_customer_message(frappe.ValidationError(native), dealer)

		self.assertNotIn("internal@micronxt.com", msg)
		self.assertIn("order.error.rep@example.com", msg)
		self.assertIn("Name: Meena", msg)
