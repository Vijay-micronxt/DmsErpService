import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.catalog.setup import setup_catalog
from dms_erp.dashboard import api as dashboard_api
from dms_erp.finance import claims_api
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import inquiry_api, order_api
from dms_erp.purchase import po_api
from dms_erp.warehouse import allocation_api, inward_api, transfer_api
from dms_erp.warehouse.setup import setup_warehouse
from dms_erp.warehouse.test_fixtures import ensure_company, make_bay, make_dealer, make_item, make_supplier


class TestDashboardApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		setup_warehouse()
		cls.supplier = make_supplier("Dashboard Test Supplier")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _priced_item(self, code, rate=500):
		item = make_item(code, "Vitrified")
		pricing_api.ensure_price_record(item, self.supplier, rate * 0.7, 20, "2026-08-01")
		pricing_api.approve_price(item=item, final_price=rate, reason="Launch")
		return item

	# ---------------- Sales ----------------

	def test_sales_dashboard_counts_and_missed_demand(self):
		item = self._priced_item("DASH-SALES-ITEM", rate=500)
		dealer = make_dealer("Dashboard Sales Dealer")

		i1 = inquiry_api.create_inquiry(dealer=dealer, item=item, qty=20, source="Phone")
		inquiry_api.update_inquiry(i1["id"], {"status": "Out of Stock"})
		i2 = inquiry_api.create_inquiry(dealer=dealer, item=item, qty=10, source="WhatsApp")
		inquiry_api.update_inquiry(i2["id"], {"status": "Pre-order Required"})
		inquiry_api.create_inquiry(dealer=dealer, item=item, qty=5, source="Phone")  # stays Open

		result = dashboard_api.sales_dashboard()

		self.assertGreaterEqual(result["todaysInquiries"], 3)
		self.assertEqual(result["missedDemandValue"], (20 + 10) * 500)
		self.assertTrue(any(a["id"] for a in result["actionableInquiries"]))

	def test_sales_dashboard_requires_sales_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			dashboard_api.sales_dashboard()

	# ---------------- Warehouse ----------------

	def test_warehouse_dashboard_kpis_and_alerts(self):
		item = self._priced_item("DASH-WH-ITEM")
		make_bay("DASH-WH-A01", categories=["Vitrified"])

		allocation_api.create_allocation(
			item=item, batch_no="DASH-WH-B1", total_qty=100, lines=[{"bay": "DASH-WH-A01", "qty": 100}], supplier=self.supplier
		)
		truck = inward_api.add_truck(supplier=self.supplier, item=item, boxes=50, lr_number="LR-DASH-1")
		inward_api.advance_truck(truck["id"], "Unloading")

		result = dashboard_api.warehouse_dashboard()

		self.assertGreaterEqual(result["kpis"]["totalBays"], 1)
		self.assertGreaterEqual(result["kpis"]["pendingAllocationsToday"], 1)
		self.assertTrue(any(a["id"] == f"dock-{truck['id']}" for a in result["alerts"]))

	def test_warehouse_dashboard_requires_warehouse_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			dashboard_api.warehouse_dashboard()

	# ---------------- Purchase ----------------

	def test_purchase_dashboard_flags_pending_and_delayed_pos(self):
		item = self._priced_item("DASH-PO-ITEM")
		po_api.create_purchase_order(
			item=item, ordered_qty=200, supplier=self.supplier, expected_ready_date=add_days(today(), -5)
		)

		result = dashboard_api.purchase_dashboard()

		self.assertGreaterEqual(result["pendingPOs"], 1)
		self.assertGreaterEqual(result["supplierDelays"], 1)
		self.assertIsInstance(result["reorderSuggestionsCount"], int)

	def test_purchase_dashboard_requires_purchase_or_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			dashboard_api.purchase_dashboard()

	# ---------------- Management ----------------

	def test_management_dashboard_top_item_and_credit_alert(self):
		item = self._priced_item("DASH-MGMT-ITEM", rate=1000)
		dealer = make_dealer("Dashboard Mgmt Dealer")
		frappe.db.set_value("Customer", dealer, "credit_limit", 500)

		inquiry = inquiry_api.create_inquiry(dealer=dealer, item=item, qty=10, source="Phone")
		order_api.create_order(dealer=dealer, lines=[{"item": item, "qty": 10}], expected_dispatch=today(), inquiry=inquiry["id"])

		result = dashboard_api.management_dashboard()

		self.assertGreaterEqual(result["totalSalesMtd"], 10000)
		self.assertIsNotNone(result["topMovingItem"])
		self.assertTrue(any(a["id"] == f"credit-{dealer}" for a in result["alerts"]))

	def test_management_dashboard_claimable_value_matches_claims_api(self):
		item = self._priced_item("DASH-CLAIM-ITEM")
		make_bay("DASH-CLM-MAIN", bay_type="main", categories=["Vitrified"])
		make_bay("DASH-CLM-DMG", bay_type="damage", categories=["Vitrified"])

		allocation_api.create_allocation(
			item=item, batch_no="DASH-CLM-B1", total_qty=30, lines=[{"bay": "DASH-CLM-MAIN", "qty": 30}], supplier=self.supplier
		)
		transfer = transfer_api.transfer_stock(
			from_bay="DASH-CLM-MAIN", to_bay="DASH-CLM-DMG", item=item, batch_no="DASH-CLM-B1", qty=10,
			transfer_type="Damage→Insurance Claim", reason="Insurance Claim", damage_type="Broken",
		)
		claims_api.file_claim(stock_entry=transfer["id"], insurer="HDFC Ergo", claim_amount=5000)

		result = dashboard_api.management_dashboard()
		self.assertGreaterEqual(result["claimableValue"], 5000)

	def test_management_dashboard_requires_management_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			dashboard_api.management_dashboard()

	def test_management_dashboard_rejects_non_management_staff_role(self):
		# Sales/Warehouse/Purchase staff shouldn't see the financial overview either.
		frappe.set_user("Administrator")
		other_user = "dashboard-sales-only@pacific.test"
		if frappe.db.exists("User", other_user):
			frappe.delete_doc("User", other_user, force=True, ignore_permissions=True)
		frappe.get_doc(
			{"doctype": "User", "email": other_user, "first_name": "Sales", "send_welcome_email": 0, "roles": [{"role": "Pacific Sales"}]}
		).insert(ignore_permissions=True)

		frappe.set_user(other_user)
		with self.assertRaises(frappe.PermissionError):
			dashboard_api.management_dashboard()
