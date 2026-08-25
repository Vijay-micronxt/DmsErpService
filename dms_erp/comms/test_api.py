import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.comms import api as comms_api
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer

TEST_WEBHOOK_SECRET = "unit-test-webhook-secret"


class TestCommsApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		frappe.conf.dms_erp_whatsapp_webhook_secret = TEST_WEBHOOK_SECRET
		cls.dealer = make_dealer("Comms Test Dealer")

	@classmethod
	def tearDownClass(cls):
		frappe.conf.pop("dms_erp_whatsapp_webhook_secret", None)
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_send_message_creates_outbound_sent(self):
		message = comms_api.send_message(dealer=self.dealer, text="Checking stock, will confirm shortly.")
		self.assertEqual(message["direction"], "Outbound")
		self.assertEqual(message["status"], "Sent")
		self.assertEqual(message["sentBy"], "Administrator")

	def test_send_message_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			comms_api.send_message(dealer=self.dealer, text="hi")

	def test_list_messages_returns_ascending_by_sent_at(self):
		comms_api.send_message(dealer=self.dealer, text="first")
		comms_api.send_message(dealer=self.dealer, text="second")

		thread = comms_api.list_messages(self.dealer)
		self.assertEqual([m["text"] for m in thread[-2:]], ["first", "second"])
		self.assertEqual(comms_api.last_message(self.dealer)["text"], "second")

	def test_webhook_inbound_message_rejects_wrong_secret(self):
		with self.assertRaises(frappe.PermissionError):
			comms_api.webhook_inbound_message(secret="wrong", dealer=self.dealer, text="hi")

	def test_webhook_inbound_message_creates_delivered_inbound(self):
		message = comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="Need 320 boxes urgently.")
		self.assertEqual(message["direction"], "Inbound")
		self.assertEqual(message["status"], "Delivered")
		self.assertIsNone(message["sentBy"])

	def test_mark_read_transitions_inbound_message(self):
		message = comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="Any update?")
		updated = comms_api.mark_read(message["id"])
		self.assertEqual(updated["status"], "Read")

	def test_webhook_status_update_progresses_outbound_message(self):
		message = comms_api.send_message(dealer=self.dealer, text="Your order is confirmed.")
		updated = comms_api.webhook_status_update(secret=TEST_WEBHOOK_SECRET, message=message["id"], status="Delivered")
		self.assertEqual(updated["status"], "Delivered")
		updated = comms_api.webhook_status_update(secret=TEST_WEBHOOK_SECRET, message=message["id"], status="Read")
		self.assertEqual(updated["status"], "Read")

	def test_webhook_status_update_rejects_inbound_message(self):
		message = comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=self.dealer, text="hi")
		with self.assertRaises(frappe.ValidationError):
			comms_api.webhook_status_update(secret=TEST_WEBHOOK_SECRET, message=message["id"], status="Read")

	def test_unreplied_inbound_count(self):
		dealer = make_dealer("Comms Test Dealer 2")
		self.assertEqual(comms_api.unreplied_inbound_count(dealer), 0)

		comms_api.webhook_inbound_message(secret=TEST_WEBHOOK_SECRET, dealer=dealer, text="Need stock update")
		self.assertEqual(comms_api.unreplied_inbound_count(dealer), 1)

		comms_api.send_message(dealer=dealer, text="Checking now")
		self.assertEqual(comms_api.unreplied_inbound_count(dealer), 0)

	def test_list_templates_returns_seeded_templates(self):
		templates = comms_api.list_templates()
		self.assertTrue(any(t["label"] == "Item available" for t in templates))
