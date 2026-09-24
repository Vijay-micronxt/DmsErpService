from frappe.tests.utils import FrappeTestCase

from dms_erp.phone_utils import clean_indian_mobile


class TestPhoneUtils(FrappeTestCase):
	def test_accepts_a_bare_ten_digit_number(self):
		self.assertEqual(clean_indian_mobile("9620204657"), "9620204657")

	def test_strips_a_plus_91_prefix_and_spaces(self):
		self.assertEqual(clean_indian_mobile("+91 96202 04657"), "9620204657")

	def test_strips_a_bare_91_country_code(self):
		self.assertEqual(clean_indian_mobile("919620204657"), "9620204657")

	def test_strips_a_leading_domestic_zero(self):
		self.assertEqual(clean_indian_mobile("09620204657"), "9620204657")

	def test_strips_dashes_and_parentheses(self):
		self.assertEqual(clean_indian_mobile("(+91)-96202-04657"), "9620204657")

	def test_rejects_a_number_not_starting_with_6_through_9(self):
		self.assertIsNone(clean_indian_mobile("5620204657"))

	def test_rejects_the_wrong_length(self):
		self.assertIsNone(clean_indian_mobile("96202046"))
		self.assertIsNone(clean_indian_mobile("962020465712"))

	def test_rejects_none_and_empty(self):
		self.assertIsNone(clean_indian_mobile(None))
		self.assertIsNone(clean_indian_mobile(""))
