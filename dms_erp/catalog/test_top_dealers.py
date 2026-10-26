import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_months, today

from dms_erp.catalog import sample_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer, make_item
from dms_erp.warehouse.utils import default_company


def _invoice(dealer, item, qty, rate, posting_date=None, is_return=0):
	"""A submitted Sales Invoice without stock (update_stock 0), as the migrated Tally ones are."""
	doc = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": dealer,
			"company": default_company(),
			"set_posting_time": 1,
			"posting_date": posting_date or today(),
			"due_date": posting_date or today(),
			"is_return": is_return,
			"items": [{"item_code": item, "qty": -qty if is_return else qty, "rate": rate}],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


class TestTopDealers(FrappeTestCase):
	"""BRD C.10.4 -- top dealers from six months of retail sales, bulk/project excluded."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		cls.item = make_item("TOPD-ITEM", "Vitrified", has_batch_no=0)
		cls.other_item = make_item("TOPD-OTHER", "Vitrified", has_batch_no=0)
		cls.big = make_dealer("Top Dealer Big Co")
		cls.small = make_dealer("Top Dealer Small Co")
		cls.bulk = make_dealer("Top Dealer Bulk Co")
		frappe.db.set_value("Customer", cls.bulk, "custom_dealer_type", "Bulk")
		cls.old = make_dealer("Top Dealer Old Co")
		_invoice(cls.big, cls.item, 100, 500)
		_invoice(cls.small, cls.other_item, 10, 500)
		_invoice(cls.bulk, cls.item, 1000, 500)
		_invoice(cls.old, cls.item, 500, 500, posting_date=add_days(add_months(today(), -6), -5))

	def _ranked(self, **kwargs):
		ours = {self.big, self.small, self.bulk, self.old}
		return [r for r in sample_api.top_dealers(**kwargs) if r["dealer"] in ours]

	def test_ranks_retail_dealers_by_six_months_of_invoices(self):
		ranked = self._ranked(limit=500)
		self.assertEqual([r["dealer"] for r in ranked], [self.big, self.small])
		self.assertEqual(ranked[0]["retailValue"], 50000)
		self.assertEqual(ranked[0]["invoices"], 1)

	def test_bulk_dealers_and_older_invoices_are_left_out(self):
		dealers = {r["dealer"] for r in self._ranked(limit=500)}
		self.assertNotIn(self.bulk, dealers)
		self.assertNotIn(self.old, dealers)

	def test_item_filter_ranks_only_buyers_of_that_item(self):
		self.assertEqual([r["dealer"] for r in self._ranked(item=self.item, limit=500)], [self.big])
