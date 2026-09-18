import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.finance import labour_api
from dms_erp.warehouse.test_fixtures import ensure_company


class TestLabourApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()

	def tearDown(self):
		frappe.set_user("Administrator")
		# DMS Accounting Settings is a Single (global state) — never leave it
		# configured for the next test.
		frappe.db.set_single_value("DMS Accounting Settings", "post_accounting_entries", 0)
		for field in ("default_company", "default_bank_account", "unloading_expense_account"):
			frappe.db.set_single_value("DMS Accounting Settings", field, None)

	def test_create_labour_record_starts_at_zero_due(self):
		record = labour_api.create_labour_record(labourer_name="Ramesh Bhai", daily_rate=500)
		self.assertEqual(record["amountDue"], 0)
		self.assertEqual(record["status"], "Pending")
		self.assertEqual(record["attendance"], [])

	def test_add_attendance_day_recomputes_amount_due_from_present_days_only(self):
		record = labour_api.create_labour_record(labourer_name="Ramesh Bhai", daily_rate=500)

		record = labour_api.add_attendance_day(record["id"], date="2026-09-01", present=True, work_done="Unloading")
		self.assertEqual(record["amountDue"], 500)

		record = labour_api.add_attendance_day(record["id"], date="2026-09-02", present=True, work_done="Movement")
		self.assertEqual(record["amountDue"], 1000)

		# An absent day doesn't add to amount due.
		record = labour_api.add_attendance_day(record["id"], date="2026-09-03", present=False)
		self.assertEqual(record["amountDue"], 1000)
		self.assertEqual(len(record["attendance"]), 3)

	def test_add_attendance_day_links_back_to_an_unloading_voucher(self):
		record = labour_api.create_labour_record(labourer_name="Ramesh Bhai", daily_rate=500)
		record = labour_api.add_attendance_day(
			record["id"], date="2026-09-01", present=True, work_done="Unloading", linked_unloading_voucher="UNL-2026-0001"
		)
		self.assertEqual(record["attendance"][0]["linkedUnloadingVoucher"], "UNL-2026-0001")

	def test_record_payment_transitions_pending_to_partially_paid_to_paid(self):
		record = labour_api.create_labour_record(labourer_name="Suresh Bhai", daily_rate=500)
		record = labour_api.add_attendance_day(record["id"], date="2026-09-01", present=True)
		record = labour_api.add_attendance_day(record["id"], date="2026-09-02", present=True)
		self.assertEqual(record["amountDue"], 1000)

		partially = labour_api.record_payment(record["id"], amount_paid=400, payment_mode="Cash")
		self.assertEqual(partially["status"], "Partially Paid")
		self.assertEqual(partially["amountPaid"], 400)

		paid = labour_api.record_payment(record["id"], amount_paid=600, payment_mode="UPI", payment_reference="UPI-REF-1")
		self.assertEqual(paid["status"], "Paid")
		self.assertEqual(paid["amountPaid"], 1000)
		self.assertEqual(paid["paymentMode"], "UPI")

	def test_record_payment_rejects_invalid_payment_mode(self):
		record = labour_api.create_labour_record(labourer_name="Ramesh Bhai", daily_rate=500)
		with self.assertRaises(frappe.ValidationError):
			labour_api.record_payment(record["id"], amount_paid=100, payment_mode="Wire Transfer")

	def test_record_payment_never_posts_when_setting_unchecked(self):
		record = labour_api.create_labour_record(labourer_name="Ramesh Bhai", daily_rate=500)
		labour_api.add_attendance_day(record["id"], date="2026-09-01", present=True)
		paid = labour_api.record_payment(record["id"], amount_paid=500, payment_mode="Cash")
		self.assertIsNone(paid["paymentEntry"])

	def test_record_payment_posts_payment_entry_when_setting_is_on(self):
		company = ensure_company()
		accounts = frappe.get_all("Account", filters={"company": company, "is_group": 0}, pluck="name", limit=2)
		if len(accounts) < 2:
			self.skipTest("Test company has no Chart of Accounts to pick two leaf accounts from.")
		bank_account, expense_account = accounts

		frappe.db.set_single_value("DMS Accounting Settings", "post_accounting_entries", 1)
		frappe.db.set_single_value("DMS Accounting Settings", "default_company", company)
		frappe.db.set_single_value("DMS Accounting Settings", "default_bank_account", bank_account)
		frappe.db.set_single_value("DMS Accounting Settings", "unloading_expense_account", expense_account)

		record = labour_api.create_labour_record(labourer_name="Ramesh Bhai", daily_rate=500)
		labour_api.add_attendance_day(record["id"], date="2026-09-01", present=True)
		paid = labour_api.record_payment(record["id"], amount_paid=500, payment_mode="Cash")

		self.assertIsNotNone(paid["paymentEntry"])
		pe = frappe.get_doc("Payment Entry", paid["paymentEntry"])
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(pe.paid_amount, 500)

	def test_write_requires_warehouse_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			labour_api.create_labour_record(labourer_name="Ramesh Bhai", daily_rate=500)

	def test_list_labour_records_is_paginated_and_filters_by_status(self):
		labour_api.create_labour_record(labourer_name="Page Labourer A", daily_rate=500)
		labour_api.create_labour_record(labourer_name="Page Labourer B", daily_rate=500)

		page = labour_api.list_labour_records(limit=1, offset=0)
		self.assertGreaterEqual(page["total"], 2)
		self.assertEqual(len(page["items"]), 1)

		pending = labour_api.list_labour_records(status="Pending")
		self.assertGreaterEqual(pending["total"], 2)
