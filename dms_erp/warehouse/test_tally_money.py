import uuid
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.warehouse import tally_money as M
from dms_erp.warehouse import tally_vouchers as V
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_supplier
from dms_erp.warehouse.test_tally_vouchers import _account

CUSTOMER = "TM Test Customer"
SUPPLIER = "TM Test Supplier"


def _voucher(vtype, ledgers, **extra):
	return {
		"guid": f"test-{uuid.uuid4()}",
		"type": vtype,
		"number": f"R/{uuid.uuid4().hex[:6]}",
		"date": "20260825",
		"narration": "Being test money",
		"ledgers": ledgers,
		"rows": [],
		**extra,
	}


def _line(ledger, amount, bills=None, instrument="", instrument_date=""):
	return {"ledger": ledger, "amount": str(amount), "party": False, "bills": bills or [], "instrument": instrument, "instrument_date": instrument_date}


class TestTallyMoney(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = ensure_company()
		make_dealer(CUSTOMER)
		make_supplier(SUPPLIER)
		cls.bank = _account(cls.company, "TM Bank", "Asset", "Bank")
		cls.other_bank = _account(cls.company, "TM Other Bank", "Asset", "Bank")
		cls.cash = _account(cls.company, "TM Cash", "Asset", "Cash")
		cls.expense = _account(cls.company, "TM Water Expense", "Expense")

	def _run(self, vouchers):
		with patch.object(V, "TALLY_VOUCHERS_ENABLED", True):
			return V.receive_tally_vouchers(vouchers, company=self.company)

	def _doc(self, out, index=0):
		made = out["created"][index]
		return frappe.get_doc(made["doctype"], made["name"])

	def test_a_bank_receipt_from_a_dealer_is_a_draft_payment_entry(self):
		out = self._run([_voucher("Bank Receipt", [_line(CUSTOMER, 9936), _line("TM Bank", -9936, instrument="CMS123", instrument_date="20260808")])])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.docstatus, doc.payment_type, doc.party_type, doc.party), ("Payment Entry", 0, "Receive", "Customer", CUSTOMER))
		self.assertEqual((doc.paid_to, doc.paid_amount, doc.received_amount), (self.bank, 9936, 9936))
		self.assertEqual((doc.reference_no, str(doc.reference_date)), ("CMS123", "2026-08-08"))
		self.assertEqual((doc.tally_voucher_type, doc.posting_date.isoformat()), ("Bank Receipt", "2026-08-25"))
		self.assertTrue(doc.tally_guid.startswith("test-"))
		self.assertEqual(out["created"][0]["amount"], 9936)

	def test_a_cash_receipt_without_a_cheque_uses_its_voucher_number(self):
		v = _voucher("Cash Receipt", [_line(CUSTOMER, 54000), _line("TM Cash", -54000)])
		doc = self._doc(self._run([v]))
		self.assertEqual((doc.paid_to, doc.reference_no, doc.reference_date.isoformat()), (self.cash, v["number"], "2026-08-25"))

	def test_a_bank_payment_to_a_supplier_is_a_pay_entry(self):
		out = self._run([_voucher("Bank Payment", [_line(SUPPLIER, -1106568), _line("TM Bank", 1106568, instrument="UTR9")])])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.payment_type, doc.party_type, doc.party), ("Payment Entry", "Pay", "Supplier", SUPPLIER))
		self.assertEqual((doc.paid_from, doc.paid_amount), (self.bank, 1106568))

	def test_an_expense_paid_in_cash_is_a_journal_entry(self):
		out = self._run([_voucher("Cash Payment", [_line("TM Water Expense", -2000), _line("TM Cash", 2000)])])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.docstatus, doc.voucher_type), ("Journal Entry", 0, "Cash Entry"))
		by_account = {r.account: (r.debit_in_account_currency, r.credit_in_account_currency) for r in doc.accounts}
		self.assertEqual(by_account, {self.expense: (2000, 0), self.cash: (0, 2000)})
		self.assertIn("Being test money", doc.user_remark)

	def test_a_bank_entry_carries_the_voucher_number_and_date_not_the_transfers(self):
		# As the migrated bank journals do: the transfer's own number stays in the narration.
		v = _voucher("Bank Payment", [_line("TM Water Expense", -750), _line("TM Bank", 750, instrument="NEFT77", instrument_date="20260820")])
		doc = self._doc(self._run([v]))
		self.assertEqual((doc.voucher_type, doc.cheque_no, doc.cheque_date.isoformat()), ("Bank Entry", v["number"], "2026-08-25"))

	def test_a_cash_entry_has_no_cheque(self):
		doc = self._doc(self._run([_voucher("Cash Payment", [_line("TM Water Expense", -20), _line("TM Cash", 20)])]))
		self.assertFalse(doc.cheque_no)

	def test_a_contra_carries_the_transfers_number_or_tallys_unique_reference_and_its_date(self):
		by_number = _voucher("Contra", [_line("TM Bank", 100, instrument="376984626", instrument_date="20260810"), _line("TM Other Bank", -100, instrument="376984626", instrument_date="20260810")])
		line = _line("TM Bank", 200, instrument_date="20260804")
		line["unique_ref"] = "5Vqc1BofxMce25JD"
		by_unique = _voucher("Contra", [line, _line("TM Other Bank", -200, instrument_date="20260804")])
		a, b = self._doc(self._run([by_number])), self._doc(self._run([by_unique]))
		self.assertEqual((a.cheque_no, a.cheque_date.isoformat()), ("376984626", "2026-08-10"))
		self.assertEqual((b.cheque_no, b.cheque_date.isoformat()), ("5Vqc1BofxMce25JD", "2026-08-04"))

	def test_a_contra_between_two_banks_is_a_contra_entry(self):
		out = self._run([_voucher("Contra", [_line("TM Bank", 30000), _line("TM Other Bank", -30000)])])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.voucher_type), ("Journal Entry", "Contra Entry"))
		by_account = {r.account: (r.debit_in_account_currency, r.credit_in_account_currency) for r in doc.accounts}
		self.assertEqual(by_account, {self.bank: (0, 30000), self.other_bank: (30000, 0)})

	def test_a_receipt_whose_money_leaves_the_bank_is_kept_as_a_journal_not_a_payment_entry(self):
		# A "receipt" that debits the dealer and credits the bank is the reverse of one; a Payment Entry
		# would book it the wrong way round, so it stays a faithful journal.
		doc = self._doc(self._run([_voucher("Bank Receipt", [_line(CUSTOMER, -500), _line("TM Bank", 500)])]))
		self.assertEqual(doc.doctype, "Journal Entry")
		party_row = next(r for r in doc.accounts if r.party)
		self.assertEqual((party_row.party_type, party_row.party, party_row.debit_in_account_currency), ("Customer", CUSTOMER, 500))

	def test_a_receipt_split_over_two_dealers_is_a_journal_with_both(self):
		other = make_dealer("TM Test Customer Two")
		doc = self._doc(self._run([_voucher("Bank Receipt", [_line(CUSTOMER, 300), _line(other, 200), _line("TM Bank", -500)])]))
		self.assertEqual(doc.doctype, "Journal Entry")
		self.assertEqual(sorted(r.party for r in doc.accounts if r.party), sorted([CUSTOMER, other]))

	def test_an_unknown_ledger_is_reported_not_invented(self):
		out = self._run([_voucher("Cash Payment", [_line("TM Ledger Nobody Has", -10), _line("TM Cash", 10)])])
		self.assertEqual(out["created"], [])
		self.assertEqual(out["unplacedTotal"], 1)
		self.assertIn("TM Ledger Nobody Has", out["unplaced"][0]["reason"])
		self.assertFalse(frappe.db.exists("Account", {"account_name": "TM Ledger Nobody Has"}))

	def test_one_ledger_line_is_not_a_voucher(self):
		out = self._run([_voucher("Cash Payment", [_line("TM Cash", 10)])])
		self.assertEqual((len(out["created"]), out["unplacedTotal"]), (0, 1))

	def test_a_voucher_already_in_the_erp_is_not_made_twice(self):
		v = _voucher("Cash Receipt", [_line(CUSTOMER, 100), _line("TM Cash", -100)])
		self.assertEqual(len(self._run([v])["created"]), 1)
		again = self._run([v])
		self.assertEqual((len(again["created"]), again["alreadyInErp"]), (0, 1))
		self.assertEqual(V.filter_new_tally_vouchers([v["guid"]]), [])

	def test_a_cancelled_receipt_is_left_alone(self):
		out = self._run([_voucher("Cash Receipt", [_line(CUSTOMER, 100), _line("TM Cash", -100)], cancelled=True)])
		self.assertEqual((out["created"], out["ignored"]), ([], {"Cancelled or optional": 1}))

	def test_a_receipt_is_allocated_to_a_submitted_invoice_for_no_more_than_it_owes(self):
		owed = [frappe._dict(name="SINV-OWED-1", outstanding_amount=700)]
		bills = [{"name": "PI/00001/26-27", "type": "Agst Ref", "amount": "1000"}]
		with patch("frappe.get_all", return_value=owed):
			refs, notes = M._open_invoices("Customer", CUSTOMER, bills, self.company)
		self.assertEqual(refs, [{"reference_doctype": "Sales Invoice", "reference_name": "SINV-OWED-1", "allocated_amount": 700}])
		self.assertIn("only 700", notes[0])

	def test_an_invoice_the_erp_does_not_have_submitted_is_noted_not_guessed(self):
		bills = [{"name": "PI/00002/26-27", "type": "Agst Ref", "amount": "1000"}, {"name": "ADV", "type": "Advance", "amount": "50"}]
		doc = self._doc(self._run([_voucher("Bank Receipt", [_line(CUSTOMER, 1000, bills=bills), _line("TM Bank", -1000, instrument="X1")])]))
		self.assertEqual(len(doc.references), 0)
		self.assertIn("PI/00002/26-27", doc.remarks)
		self.assertNotIn("ADV", doc.remarks)

	def test_a_receipt_that_nets_another_document_off_allocates_nothing(self):
		# Real Tally voucher: 54,000 received, bills at -63,600 (an earlier receipt), +63,600 and +54,000.
		bills = [
			{"name": "CR/0007/26-27", "type": "Agst Ref", "amount": "-63600"},
			{"name": "PI/01643/26-27", "type": "Agst Ref", "amount": "63600"},
			{"name": "PI/01868/26-27", "type": "Agst Ref", "amount": "54000"},
		]
		with patch.object(M, "_find_bill", return_value=("Sales Invoice", "SINV-X", 99999)):
			doc = self._doc(self._run([_voucher("Cash Receipt", [_line(CUSTOMER, 54000, bills=bills), _line("TM Cash", -54000)])]))
		self.assertEqual((len(doc.references), doc.difference_amount), (0, 0))
		self.assertIn("netted", doc.remarks)

	def test_bills_that_add_up_to_more_than_was_paid_are_not_allocated(self):
		bills = [{"name": "A", "type": "Agst Ref", "amount": "800"}, {"name": "B", "type": "Agst Ref", "amount": "800"}]
		with patch.object(M, "_find_bill", return_value=("Sales Invoice", "SINV-X", 99999)):
			doc = self._doc(self._run([_voucher("Cash Receipt", [_line(CUSTOMER, 1000, bills=bills), _line("TM Cash", -1000)])]))
		self.assertEqual(len(doc.references), 0)

	def test_a_payment_settling_a_freight_bill_journal_entry_is_allocated_to_it(self):
		bills = [{"name": "FRT/1/26-27", "type": "Agst Ref", "amount": "-5000"}]
		with patch.object(M, "_find_bill", return_value=("Journal Entry", "ACC-JV-X", 5000)):
			refs, _ = M._open_invoices("Supplier", SUPPLIER, bills, self.company)
		self.assertEqual(refs, [{"reference_doctype": "Journal Entry", "reference_name": "ACC-JV-X", "allocated_amount": 5000}])
