import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.sales import dealer_activity
from dms_erp.sales.setup import setup_sales
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item


def _set_activity_inquiry_date(dealer: str, item: str, days_ago: int):
	from dms_erp.sales import inquiry_api

	inquiry = inquiry_api.create_inquiry(dealer=dealer, item=item, qty=1, source="Phone")
	frappe.db.set_value("Inquiry", inquiry["id"], "date", add_days(today(), -days_ago))


def _notification_count(user: str, fragment: str) -> int:
	return frappe.db.count("Notification Log", {"for_user": user, "subject": ["like", f"%{fragment}%"]})


class TestDealerActivity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_sales()
		cls.item = make_item("ACT-TEST-ITEM", "Vitrified")

	def _dealer(self, name: str) -> str:
		dealer = make_dealer(name)
		frappe.db.set_value("Customer", dealer, "custom_phone", "9" + str(abs(hash(name)))[:9].ljust(9, "0"))
		return dealer

	def test_dormant_dealer_is_flagged_and_recent_dealer_is_not(self):
		dormant = self._dealer("Activity Dormant Dealer")
		recent = self._dealer("Activity Recent Dealer")
		_set_activity_inquiry_date(dormant, self.item, 200)
		_set_activity_inquiry_date(recent, self.item, 5)

		dealer_activity.flag_inactive_dealers()

		self.assertEqual(frappe.db.get_value("Customer", dormant, "custom_inactive"), 1)
		self.assertEqual(frappe.db.get_value("Customer", recent, "custom_inactive"), 0)
		self.assertEqual(str(frappe.db.get_value("Customer", dormant, "custom_last_activity_on")), add_days(today(), -200))

	def test_a_brand_new_dealer_with_no_activity_is_not_inactive(self):
		dealer = self._dealer("Activity Brand New Dealer")
		dealer_activity.flag_inactive_dealers()
		self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_inactive"), 0)

	def test_old_creation_date_never_masks_real_dormancy(self):
		# Regression for the real finding: the Tally migration created every Customer
		# on one day, so creation must not floor real (old) activity.
		dealer = self._dealer("Activity Migrated Dealer")
		_set_activity_inquiry_date(dealer, self.item, 400)
		frappe.db.set_value("Customer", dealer, "creation", add_days(today(), -1), update_modified=False)

		dealer_activity.flag_inactive_dealers()

		self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_inactive"), 1)

	def test_dealer_clears_the_flag_once_active_again(self):
		dealer = self._dealer("Activity Returning Dealer")
		_set_activity_inquiry_date(dealer, self.item, 200)
		dealer_activity.flag_inactive_dealers()
		self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_inactive"), 1)

		_set_activity_inquiry_date(dealer, self.item, 0)
		dealer_activity.flag_inactive_dealers()

		self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_inactive"), 0)

	def test_only_the_transition_into_inactive_notifies(self):
		dealer = self._dealer("Activity Notify Once Dealer")
		_set_activity_inquiry_date(dealer, self.item, 200)
		frappe.db.set_single_value("DMS Sales Settings", "inactive_dealer_notify_user", "Administrator")

		first = dealer_activity.flag_inactive_dealers()
		second = dealer_activity.flag_inactive_dealers()

		self.assertGreaterEqual(first["newlyInactive"], 1)
		self.assertEqual(first["notified"], 1)
		self.assertEqual(second["newlyInactive"], 0)
		self.assertEqual(second["notified"], 0)

	def test_active_dealer_numbers_excludes_inactive_and_phoneless_dealers(self):
		active = self._dealer("Activity Listed Dealer")
		dormant = self._dealer("Activity Unlisted Dealer")
		phoneless = make_dealer("Activity Phoneless Dealer")
		frappe.db.set_value("Customer", phoneless, "custom_phone", None)
		_set_activity_inquiry_date(active, self.item, 3)
		_set_activity_inquiry_date(dormant, self.item, 300)
		dealer_activity.flag_inactive_dealers()

		ids = {row["dealerId"] for row in dealer_activity.active_dealer_numbers()}

		self.assertIn(active, ids)
		self.assertNotIn(dormant, ids)
		self.assertNotIn(phoneless, ids)

	def test_threshold_is_configurable(self):
		dealer = self._dealer("Activity Custom Threshold Dealer")
		_set_activity_inquiry_date(dealer, self.item, 40)
		frappe.db.set_single_value("DMS Sales Settings", "dealer_inactivity_days", 30)
		try:
			dealer_activity.flag_inactive_dealers()
			self.assertEqual(frappe.db.get_value("Customer", dealer, "custom_inactive"), 1)
		finally:
			frappe.db.set_single_value("DMS Sales Settings", "dealer_inactivity_days", 90)
