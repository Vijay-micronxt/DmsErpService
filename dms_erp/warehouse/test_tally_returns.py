from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.warehouse import tally_vouchers as V
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_supplier
from dms_erp.warehouse.test_tally_vouchers import (
	CUSTOMER,
	ITEM,
	SUPPLIER,
	_account,
	_item,
	_row,
	_voucher,
)


def _ledgers(party, party_amount, *others):
	return [{"ledger": party, "amount": str(party_amount), "party": True}] + [{"ledger": n, "amount": str(a), "party": False} for n, a in others]


class TestTallyReturns(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = ensure_company()
		_item(ITEM, 1)
		make_dealer(CUSTOMER)
		make_supplier(SUPPLIER)
		cls.sales_return = _account(cls.company, "TV Sales Return", "Income")
		cls.damages = _account(cls.company, "TV Damages Claim", "Expense")
		cls.rate_diff = _account(cls.company, "TV Rate Difference", "Expense")
		cls.cgst = _account(cls.company, "TV Output CGST", "Liability", "Tax")
		cls.input_igst = _account(cls.company, "TV Input IGST", "Asset", "Tax")
		cls.wh = frappe.db.get_value("Warehouse", {"warehouse_name": "TV Godown", "company": cls.company}) or (
			frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "TV Godown", "company": cls.company}).insert(ignore_permissions=True).name
		)
		cls.batch = "TV-B1" if frappe.db.exists("Batch", "TV-B1") else frappe.get_doc({"doctype": "Batch", "batch_id": "TV-B1", "item": ITEM}).insert(ignore_permissions=True).name

	def _run(self, vouchers):
		with patch.object(V, "TALLY_VOUCHERS_ENABLED", True), patch.object(V, "piece_unit", return_value=("peices", 9)):
			return V.receive_tally_vouchers(vouchers, company=self.company)

	def _doc(self, out, index=0):
		made = out["created"][index]
		return frappe.get_doc(made["doctype"], made["name"])

	def test_a_credit_note_with_stock_is_a_sales_invoice_return_that_brings_the_stock_back(self):
		# Tally: lines negative, party credited, the tax a debit (negative)
		ledgers = _ledgers(CUSTOMER, 5900, ("TV Output CGST", -900))
		out = self._run([_voucher("Credit Note", [_row(amount=-5000, account="TV Sales Return")], party=CUSTOMER, ledgers=ledgers)])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.docstatus, doc.is_return, doc.update_stock, doc.customer), ("Sales Invoice", 0, 1, 1, CUSTOMER))
		row = doc.items[0]
		self.assertEqual((row.qty, row.rate, row.warehouse, row.batch_no, row.income_account), (-10, 500, self.wh, self.batch, self.sales_return))
		self.assertEqual([(t.account_head, t.tax_amount) for t in doc.taxes], [(self.cgst, -900)])
		made = out["created"][0]
		self.assertEqual((made["tallyTotal"], made["erpTotal"], made["difference"]), (5900, 5900, 0))

	def test_the_return_ledger_listed_among_the_ledgers_is_not_taken_for_a_charge(self):
		ledgers = _ledgers(CUSTOMER, 5900, ("TV Sales Return", -5000), ("TV Output CGST", -900))
		doc = self._doc(self._run([_voucher("Credit Note", [_row(amount=-5000, account="TV Sales Return")], party=CUSTOMER, ledgers=ledgers)]))
		self.assertEqual([t.account_head for t in doc.taxes], [self.cgst])

	def test_a_credit_note_against_a_delivery_note_moves_no_stock(self):
		doc = self._doc(self._run([_voucher("Credit Note", [_row(amount=-5000, tracking="1637/26-27", account="TV Sales Return")], party=CUSTOMER)]))
		self.assertEqual((doc.is_return, doc.update_stock), (1, 0))

	def test_a_debit_note_with_stock_is_a_purchase_invoice_return_that_takes_the_stock_out(self):
		# Tally: lines positive, party debited (negative), the tax a credit (positive)
		ledgers = _ledgers(SUPPLIER, -5900, ("TV Damages Claim", 5000), ("TV Input IGST", 900))
		out = self._run([_voucher("Debit Note", [_row(amount=5000, account="TV Damages Claim")], party=SUPPLIER, ledgers=ledgers)])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.is_return, doc.update_stock, doc.supplier), ("Purchase Invoice", 1, 1, SUPPLIER))
		self.assertEqual((doc.items[0].qty, doc.items[0].rate, doc.items[0].expense_account), (-10, 500, self.damages))
		self.assertEqual([(t.account_head, t.tax_amount) for t in doc.taxes], [(self.input_igst, -900)])
		self.assertEqual(out["created"][0]["difference"], 0)

	def test_a_note_of_only_ledgers_is_ledger_lines_with_no_stock(self):
		ledgers = _ledgers(SUPPLIER, -1180, ("TV Rate Difference", 1000), ("TV Input IGST", 180))
		out = self._run([_voucher("Debit Note", [], party=SUPPLIER, ledgers=ledgers)])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.is_return, doc.update_stock), (1, 0))
		line = doc.items[0]
		self.assertEqual((line.item_code, line.qty, line.rate, line.expense_account), (V.LEDGER_LINE_ITEM, -1, 1000, self.rate_diff))
		self.assertEqual([(t.account_head, t.tax_amount) for t in doc.taxes], [(self.input_igst, -180)])
		self.assertEqual(out["created"][0]["difference"], 0)
		self.assertEqual(frappe.db.get_value("Item", V.LEDGER_LINE_ITEM, "is_stock_item"), 0)

	def test_a_credit_note_of_only_ledgers_books_to_an_income_account(self):
		ledgers = _ledgers(CUSTOMER, 590, ("TV Sales Return", -500), ("TV Output CGST", -90))
		doc = self._doc(self._run([_voucher("Credit Note", [], party=CUSTOMER, ledgers=ledgers)]))
		self.assertEqual((doc.doctype, doc.items[0].income_account, doc.items[0].qty), ("Sales Invoice", self.sales_return, -1))

	def test_tallys_other_two_note_types_follow_the_migration_not_the_name(self):
		# "Credit Note ( Purchase)" (Tally's own spacing) is still a Sales Invoice return, and
		# "Debit Note (Sales)" a Purchase Invoice return, as the migration booked them.
		credit = self._run([_voucher("Credit Note ( Purchase)", [], party=CUSTOMER, ledgers=_ledgers(CUSTOMER, 1180, ("TV Rate Difference", -1000), ("TV Output CGST", -180)))])
		debit = self._run([_voucher("Debit Note (Sales)", [], party=SUPPLIER, ledgers=_ledgers(SUPPLIER, -1180, ("TV Rate Difference", 1000), ("TV Input IGST", 180)))])
		self.assertEqual((self._doc(credit).doctype, self._doc(debit).doctype), ("Sales Invoice", "Purchase Invoice"))
		self.assertEqual((self._doc(credit).is_return, self._doc(debit).is_return), (1, 1))

	def test_a_debit_notes_extra_charge_on_the_same_ledger_as_its_lines_is_kept(self):
		# Real shape (DN/0019): "Damages Claim" is listed twice -- once for the stock lines, once as an extra.
		ledgers = [
			{"ledger": SUPPLIER, "amount": "-7000", "party": True},
			{"ledger": "TV Damages Claim", "amount": "5000", "party": False},
			{"ledger": "TV Damages Claim", "amount": "2000", "party": False},
		]
		doc = self._doc(self._run([_voucher("Debit Note", [_row(amount=5000, account="TV Damages Claim")], party=SUPPLIER, ledgers=ledgers)]))
		self.assertEqual([(t.account_head, t.tax_amount) for t in doc.taxes], [(self.damages, -2000)])

	def test_a_note_for_a_party_the_erp_lacks_is_reported(self):
		out = self._run([_voucher("Credit Note", [_row(amount=-5000)], party="TV Nobody Ltd")])
		self.assertEqual((out["created"], out["unplacedTotal"]), ([], 1))
		self.assertIn("TV Nobody Ltd", out["unplaced"][0]["reason"])

	def test_a_note_with_lines_part_against_a_delivery_note_is_left_for_a_person(self):
		rows = [_row(amount=-2500, tracking="1637/26-27"), _row(amount=-2500)]
		out = self._run([_voucher("Credit Note", rows, party=CUSTOMER)])
		self.assertEqual((out["created"], out["unplacedTotal"]), ([], 1))

	def test_a_cancelled_note_is_left_alone(self):
		out = self._run([_voucher("Debit Note", [], party=SUPPLIER, cancelled=True)])
		self.assertEqual((out["created"], out["ignored"]), ([], {"Cancelled or optional": 1}))
