import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.finance import claims_api
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse import allocation_api, transfer_api
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

	def test_claim_summary_aggregates_by_status(self):
		se1 = self._make_damage_transfer("CLAIM-BATCH-5")
		se2 = self._make_damage_transfer("CLAIM-BATCH-6")
		c1 = claims_api.file_claim(stock_entry=se1, insurer="HDFC Ergo", claim_amount=1000)
		c2 = claims_api.file_claim(stock_entry=se2, insurer="HDFC Ergo", claim_amount=500)
		claims_api.update_claim_status(c2["id"], "Settled", settled_amount=450)

		summary = claims_api.claim_summary()
		self.assertGreaterEqual(summary["receivable"], 1000)
		self.assertGreaterEqual(summary["settled"], 450)

	def test_write_requires_warehouse_or_management_role(self):
		stock_entry = self._make_damage_transfer("CLAIM-BATCH-7")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			claims_api.file_claim(stock_entry=stock_entry, insurer="HDFC Ergo", claim_amount=100)
