import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.catalog.setup import setup_catalog
from dms_erp.finance import claims_api, unloading_api
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.reports import finance_reports
from dms_erp.warehouse import allocation_api, inward_api, transfer_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_item, make_supplier


class TestFinanceReports(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Finance Reports Test Supplier")
		cls.item = make_item("FREPORT-TEST-ITEM", "Vitrified")
		pricing_api.ensure_price_record(cls.item, cls.supplier, 300, 20, "2026-08-01")
		cls.main_bay = make_bay("FREPORT-MAIN-01", bay_type="main", categories=["Vitrified"])
		cls.damage_bay = make_bay("FREPORT-DMG-01", bay_type="damage", categories=["Vitrified"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def _file_claim(self, batch, claim_amount=1000):
		allocation_api.create_allocation(
			item=self.item, batch_no=batch, total_qty=30, lines=[{"bay": "FREPORT-MAIN-01", "qty": 30}], supplier=self.supplier
		)
		transfer = transfer_api.transfer_stock(
			from_bay="FREPORT-MAIN-01", to_bay="FREPORT-DMG-01", item=self.item, batch_no=batch, qty=10,
			transfer_type="Damage→Insurance Claim", reason="Insurance Claim", damage_type="Broken",
		)
		return claims_api.file_claim(stock_entry=transfer["id"], insurer="HDFC Ergo", claim_amount=claim_amount)

	def test_damage_and_insurance_report_filters_by_insurer(self):
		claim = self._file_claim("FREPORT-BATCH-1")

		result = finance_reports.damage_and_insurance_report(insurer="HDFC Ergo")
		ids = {r["id"] for r in result["rows"]}
		self.assertIn(claim["id"], ids)
		self.assertGreaterEqual(result["summary"]["totalClaimed"], 1000)

		none_result = finance_reports.damage_and_insurance_report(insurer="No Such Insurer")
		self.assertEqual(none_result["summary"]["count"], 0)

	def test_claimable_value_report_breaks_down_by_insurer(self):
		claim = self._file_claim("FREPORT-BATCH-2", claim_amount=500)
		claims_api.update_claim_status(claim["id"], "Settled", settled_amount=450)

		result = finance_reports.claimable_value_report()
		bucket = next(b for b in result["byInsurer"] if b["insurer"] == "HDFC Ergo")
		self.assertGreaterEqual(bucket["settled"], 450)

	def test_unloading_payment_report_summarizes_pending_and_paid(self):
		truck = inward_api.add_truck(supplier=self.supplier, item=self.item, boxes=200, lr_number="LR-FREPORT-1")
		charge = unloading_api.record_charge(inward_truck=truck["id"], contractor="Morbi Labour Contractors", rate_per_box=5, payment_mode="Cash")

		result = finance_reports.unloading_payment_report(contractor="Morbi Labour Contractors")
		self.assertGreaterEqual(result["summary"]["totalPending"], charge["chargeAmount"])

		unloading_api.mark_paid(charge["id"])
		paid_result = finance_reports.unloading_payment_report(status="Paid")
		ids = {r["id"] for r in paid_result["rows"]}
		self.assertIn(charge["id"], ids)
