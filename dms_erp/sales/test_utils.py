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


class TestResolveTaxForDealer(FrappeTestCase):
	"""Deepak's GST rule: bill-to address state vs company, else GSTIN prefix, else In-State.
	The company side is patched so the cases don't depend on this site's Company Address."""

	def setUp(self):
		super().setUp()
		from unittest import mock

		patches = [
			mock.patch("dms_erp.sales.utils.company_gst_identity", return_value=("Karnataka", "29")),
			mock.patch("dms_erp.sales.utils._template_for_scope", side_effect=lambda scope, company: f"T-{scope}"),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		self.n = 0

	def _dealer(self, gstin=None):
		self.n += 1
		name = make_dealer(f"Tax Rule Dealer {self.n}")
		if gstin:
			frappe.db.set_value("Customer", name, "custom_gstin", gstin)
		return name

	def _address(self, dealer, kind, state, flagged=0, disabled=0, title="a"):
		self.n += 1
		doc = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": f"{title}{self.n}",
				"address_type": kind,
				"address_line1": "1 Test Rd",
				"city": "Testville",
				"state": state,
				"gst_state": state,
				"country": "India",
				"pincode": "600001" if state == "Tamil Nadu" else "560001",
				"is_primary_address": flagged if kind == "Billing" else 0,
				"is_shipping_address": flagged if kind == "Shipping" else 0,
				"disabled": disabled,
				"links": [{"link_doctype": "Customer", "link_name": dealer}],
			}
		)
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		return doc.name

	def test_billing_state_equal_to_company_is_in_state(self):
		from dms_erp.sales.utils import resolve_tax_for_dealer

		dealer = self._dealer()
		self._address(dealer, "Billing", "Karnataka", flagged=1)
		result = resolve_tax_for_dealer(dealer)
		self.assertEqual((result["scope"], result["template"], result["basis"]), ("In-State", "T-In-State", "address-state"))
		self.assertFalse(result["assumed"])

	def test_other_state_is_out_state(self):
		from dms_erp.sales.utils import resolve_tax_for_dealer

		dealer = self._dealer()
		self._address(dealer, "Billing", "Tamil Nadu", flagged=1)
		self.assertEqual(resolve_tax_for_dealer(dealer)["template"], "T-Out-State")

	def test_shipping_is_used_only_when_no_billing_address(self):
		from dms_erp.sales.utils import resolve_tax_for_dealer

		dealer = self._dealer()
		self._address(dealer, "Shipping", "Tamil Nadu", flagged=1)
		result = resolve_tax_for_dealer(dealer)
		self.assertEqual((result["addressType"], result["scope"]), ("Shipping", "Out-State"))

	def test_disabled_billing_is_ignored(self):
		from dms_erp.sales.utils import resolve_tax_for_dealer

		dealer = self._dealer()
		self._address(dealer, "Billing", "Karnataka", flagged=1, disabled=1)
		self._address(dealer, "Shipping", "Tamil Nadu", flagged=1)
		self.assertEqual(resolve_tax_for_dealer(dealer)["addressType"], "Shipping")

	def test_flagged_billing_beats_a_newer_unflagged_one(self):
		from dms_erp.sales.utils import resolve_tax_for_dealer

		dealer = self._dealer()
		self._address(dealer, "Billing", "Karnataka", flagged=1)
		self._address(dealer, "Billing", "Tamil Nadu", flagged=0)
		self.assertEqual(resolve_tax_for_dealer(dealer)["scope"], "In-State")

	def test_unflagged_billing_addresses_take_the_latest(self):
		from dms_erp.sales.utils import resolve_tax_for_dealer

		dealer = self._dealer()
		self._address(dealer, "Billing", "Karnataka")
		self._address(dealer, "Billing", "Tamil Nadu")
		self.assertEqual(resolve_tax_for_dealer(dealer)["scope"], "Out-State")

	def test_gstin_prefix_decides_when_address_has_no_state(self):
		from dms_erp.sales.utils import resolve_tax_for_dealer

		dealer = self._dealer(gstin="33ABAPY4895N1ZU")
		addr = self._address(dealer, "Billing", "Karnataka", flagged=1)
		frappe.db.set_value("Address", addr, {"state": None, "gst_state": None})
		result = resolve_tax_for_dealer(dealer)
		self.assertEqual((result["scope"], result["basis"]), ("Out-State", "gstin"))

	def test_nothing_known_defaults_to_in_state_and_says_so(self):
		from dms_erp.sales.utils import resolve_tax_for_dealer

		result = resolve_tax_for_dealer(self._dealer())
		self.assertEqual((result["scope"], result["basis"], result["assumed"]), ("In-State", "default", True))
		self.assertTrue(result["warning"])

	def test_auto_keyword_resolves_but_none_stays_untaxed(self):
		from dms_erp.sales.utils import resolve_tax_template_arg

		dealer = self._dealer()
		self._address(dealer, "Billing", "Tamil Nadu", flagged=1)
		self.assertEqual(resolve_tax_template_arg(dealer, "auto"), "T-Out-State")
		self.assertIsNone(resolve_tax_template_arg(dealer, None))
		self.assertEqual(resolve_tax_template_arg(dealer, "Some Template"), "Some Template")
