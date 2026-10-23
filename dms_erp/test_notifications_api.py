import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp import notifications_api

OTHER_USER = "notif-other@example.com"


def _notify(user: str, subject: str, document_type: str | None = None, document_name: str | None = None) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Notification Log",
			"for_user": user,
			"type": "Alert",
			"subject": subject,
			"document_type": document_type,
			"document_name": document_name,
		}
	).insert(ignore_permissions=True)
	return doc.name


class TestNotificationsApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("User", OTHER_USER):
			frappe.get_doc(
				{"doctype": "User", "email": OTHER_USER, "first_name": "Notif Other", "send_welcome_email": 0}
			).insert(ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Notification Log", {"for_user": ["in", ["Administrator", OTHER_USER]]})

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_lists_only_my_notifications_newest_first_with_unread_count(self):
		_notify("Administrator", "First")
		_notify("Administrator", "<b>Second</b> one", "Approval Request", "APR-1")
		_notify(OTHER_USER, "Not mine")

		result = notifications_api.list_my_notifications()

		self.assertEqual([n["subject"] for n in result["items"]], ["Second one", "First"])
		self.assertEqual(result["unread"], 2)
		self.assertEqual(result["items"][0]["documentType"], "Approval Request")
		self.assertEqual(result["items"][0]["documentName"], "APR-1")

	def test_mark_one_read_and_mark_all_read(self):
		first = _notify("Administrator", "First")
		_notify("Administrator", "Second")

		self.assertEqual(notifications_api.mark_notifications_read([first])["unread"], 1)
		self.assertTrue(frappe.db.get_value("Notification Log", first, "read"))
		self.assertEqual(notifications_api.mark_notifications_read()["unread"], 0)

	def test_cannot_mark_another_users_notification(self):
		theirs = _notify(OTHER_USER, "Not mine")

		notifications_api.mark_notifications_read([theirs])

		self.assertFalse(frappe.db.get_value("Notification Log", theirs, "read"))
