import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.finance import claims_api
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse import allocation_api, stock_api, transfer_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_item, make_supplier


class TestClaimsApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Claims Test Supplier")
		cls.item = make_item("CLAIM-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		cls.main_bay = make_bay("CLAIM-MAIN-01", bay_type="main", categories=["Vitrified"])
		cls.buffer_bay = make_bay("CLAIM-BUF-01", bay_type="buffer", categories=["Vitrified"])
		cls.damage_bay = make_bay("CLAIM-DMG-01", bay_type="damage", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")
		# DMS Accounting Settings is a Single (global state) — never leave it
		# configured for the next test.
		frappe.db.set_single_value("DMS Accounting Settings", "post_accounting_entries", 0)
		for field in (
			"default_company",
			"default_bank_account",
			"insurance_claim_receivable_account",
			"insurance_settlement_variance_account",
		):
			frappe.db.set_single_value("DMS Accounting Settings", field, None)

	def _make_damage_transfer(self, batch, qty=20):
		allocation_api.create_allocation(
			item=self.item, batch_no=batch, total_qty=qty + 10, lines=[{"bay": "CLAIM-MAIN-01", "qty": qty + 10}], supplier=self.supplier
		)
		transfer = transfer_api.transfer_stock(
			from_bay="CLAIM-MAIN-01",
			to_bay="CLAIM-DMG-01",
			item=self.item,
			batch_no=batch,
			qty=qty,
			transfer_type="Damage→Insurance Claim",
			reason="Insurance Claim",
			damage_type="Broken",
		)
		return transfer["id"]

	def test_file_claim_snapshots_transfer_and_links_back(self):
		stock_entry = self._make_damage_transfer("CLAIM-BATCH-1")
		claim = claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=25600, remarks="Transit damage")

		self.assertEqual(claim["itemCode"], self.item)
		self.assertEqual(claim["batchNumber"], "CLAIM-BATCH-1")
		self.assertEqual(claim["qty"], 20)
		self.assertEqual(claim["status"], "Filed")
		self.assertEqual(frappe.db.get_value("Stock Entry", stock_entry, "custom_claim_ref"), claim["id"])

	def test_file_claim_rejects_duplicate_for_same_transfer(self):
		stock_entry = self._make_damage_transfer("CLAIM-BATCH-2")
		claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=1000)
		with self.assertRaises(frappe.DuplicateEntryError):
			claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=1000)

	def test_file_claim_rejects_non_damage_transfer(self):
		allocation_api.create_allocation(
			item=self.item, batch_no="CLAIM-BATCH-3", total_qty=30, lines=[{"bay": "CLAIM-MAIN-01", "qty": 30}], supplier=self.supplier
		)
		ordinary_transfer = transfer_api.transfer_stock(
			from_bay="CLAIM-MAIN-01", to_bay="CLAIM-BUF-01", item=self.item, batch_no="CLAIM-BATCH-3", qty=10,
			transfer_type="Main→Buffer", reason="Consolidation",
		)
		with self.assertRaises(frappe.ValidationError):
			claims_api.file_claim(stock_entry=ordinary_transfer["id"], insurer="HDFC Ergo", claim_amount=500)

	def test_update_claim_status_settles_with_amount(self):
		stock_entry = self._make_damage_transfer("CLAIM-BATCH-4")
		claim = claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=1000)

		settled = claims_api.update_claim_status(claim["id"], "Settled", settled_amount=900)
		self.assertEqual(settled["status"], "Settled")
		self.assertEqual(settled["settledAmount"], 900)
		self.assertIsNotNone(settled["settledAt"])

	def test_update_claim_status_never_posts_when_setting_unchecked(self):
		stock_entry = self._make_damage_transfer("CLAIM-BATCH-4B")
		claim = claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=1000)

		settled = claims_api.update_claim_status(claim["id"], "Settled", settled_amount=900)
		self.assertIsNone(settled["settlementJournalEntry"])

	def test_update_claim_status_rejects_when_posting_on_but_accounts_missing(self):
		frappe.db.set_single_value("DMS Accounting Settings", "post_accounting_entries", 1)

		stock_entry = self._make_damage_transfer("CLAIM-BATCH-4C")
		claim = claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=1000)

		with self.assertRaises(frappe.ValidationError):
			claims_api.update_claim_status(claim["id"], "Settled", settled_amount=900)
		self.assertEqual(claims_api.get_claim(claim["id"])["status"], "Filed")

	def test_update_claim_status_posts_journal_entry_with_variance(self):
		company = ensure_company()
		accounts = frappe.get_all("Account", filters={"company": company, "is_group": 0}, pluck="name", limit=3)
		if len(accounts) < 3:
			self.skipTest("Test company has no Chart of Accounts to pick three leaf accounts from.")
		bank_account, receivable_account, variance_account = accounts

		frappe.db.set_single_value("DMS Accounting Settings", "post_accounting_entries", 1)
		frappe.db.set_single_value("DMS Accounting Settings", "default_company", company)
		frappe.db.set_single_value("DMS Accounting Settings", "default_bank_account", bank_account)
		frappe.db.set_single_value("DMS Accounting Settings", "insurance_claim_receivable_account", receivable_account)
		frappe.db.set_single_value("DMS Accounting Settings", "insurance_settlement_variance_account", variance_account)

		stock_entry = self._make_damage_transfer("CLAIM-BATCH-4D")
		claim = claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=1000)

		settled = claims_api.update_claim_status(claim["id"], "Settled", settled_amount=900)
		self.assertIsNotNone(settled["settlementJournalEntry"])

		je = frappe.get_doc("Journal Entry", settled["settlementJournalEntry"])
		self.assertEqual(je.docstatus, 1)
		by_account = {row.account: (row.debit_in_account_currency, row.credit_in_account_currency) for row in je.accounts}
		self.assertEqual(by_account[bank_account], (900, 0))
		self.assertEqual(by_account[receivable_account], (0, 1000))
		self.assertEqual(by_account[variance_account], (100, 0))  # shortfall, claimed - settled

	def test_update_claim_status_rejects_variance_without_variance_account(self):
		company = ensure_company()
		accounts = frappe.get_all("Account", filters={"company": company, "is_group": 0}, pluck="name", limit=2)
		if len(accounts) < 2:
			self.skipTest("Test company has no Chart of Accounts to pick two leaf accounts from.")
		bank_account, receivable_account = accounts

		frappe.db.set_single_value("DMS Accounting Settings", "post_accounting_entries", 1)
		frappe.db.set_single_value("DMS Accounting Settings", "default_company", company)
		frappe.db.set_single_value("DMS Accounting Settings", "default_bank_account", bank_account)
		frappe.db.set_single_value("DMS Accounting Settings", "insurance_claim_receivable_account", receivable_account)
		# insurance_settlement_variance_account deliberately left unset.

		stock_entry = self._make_damage_transfer("CLAIM-BATCH-4E")
		claim = claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=1000)

		with self.assertRaises(frappe.ValidationError):
			claims_api.update_claim_status(claim["id"], "Settled", settled_amount=900)

	def test_claim_summary_aggregates_by_status(self):
		se1 = self._make_damage_transfer("CLAIM-BATCH-5")
		se2 = self._make_damage_transfer("CLAIM-BATCH-6")
		c1 = claims_api.file_claim(stock_entry=se1, insurer="HDFC Ergo", claim_amount=1000)
		c2 = claims_api.file_claim(stock_entry=se2, insurer="HDFC Ergo", claim_amount=500)
		claims_api.update_claim_status(c2["id"], "Settled", settled_amount=450)

		summary = claims_api.claim_summary()
		self.assertGreaterEqual(summary["receivable"], 1000)
		self.assertGreaterEqual(summary["settled"], 450)

	def test_list_claims_is_paginated_and_filters_by_status(self):
		se1 = self._make_damage_transfer("CLAIM-BATCH-PAGE-1")
		se2 = self._make_damage_transfer("CLAIM-BATCH-PAGE-2")
		se3 = self._make_damage_transfer("CLAIM-BATCH-PAGE-3")
		c1 = claims_api.file_claim(stock_entry=se1, insurer="HDFC Ergo", claim_amount=100)
		claims_api.file_claim(stock_entry=se2, insurer="HDFC Ergo", claim_amount=200)
		claims_api.file_claim(stock_entry=se3, insurer="HDFC Ergo", claim_amount=300)
		claims_api.update_claim_status(c1["id"], "Settled", settled_amount=100)

		page = claims_api.list_claims(limit=2, offset=0)
		self.assertGreaterEqual(page["total"], 3)
		self.assertEqual(len(page["items"]), 2)
		self.assertEqual(page["limit"], 2)
		self.assertEqual(page["offset"], 0)

		by_status = claims_api.list_claims(status="Settled")
		self.assertEqual(by_status["total"], 1)
		self.assertEqual(by_status["items"][0]["id"], c1["id"])

	def test_write_requires_warehouse_or_management_role(self):
		stock_entry = self._make_damage_transfer("CLAIM-BATCH-7")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=100)

	def test_list_stock_lots_exposes_damage_type_and_claim_ref(self):
		stock_entry = self._make_damage_transfer("CLAIM-BATCH-8")

		before = [l for l in stock_api.list_stock(bay="CLAIM-DMG-01") if l["batchNumber"] == "CLAIM-BATCH-8"][0]
		self.assertEqual(before["damageType"], "damage")
		self.assertIsNone(before["claimRef"])

		claim = claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=2000)

		after = [l for l in stock_api.list_stock(bay="CLAIM-DMG-01") if l["batchNumber"] == "CLAIM-BATCH-8"][0]
		self.assertEqual(after["claimRef"], claim["id"])

		main_lot = [l for l in stock_api.list_stock(bay="CLAIM-MAIN-01") if l["batchNumber"] != "CLAIM-BATCH-8"]
		if main_lot:
			self.assertIsNone(main_lot[0]["damageType"])
			self.assertIsNone(main_lot[0]["claimRef"])
