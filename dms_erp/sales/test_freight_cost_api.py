import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_months, today

from dms_erp.sales import freight_cost_api


class TestFreightCostApi(FrappeTestCase):
	"""MOM §3.1 -- 6-month average freight cost per kg by transport type, staff only."""

	def setUp(self):
		frappe.db.delete("Freight Cost Entry")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _add(self, transport_type, weight_kg, freight_cost, entry_date=None):
		return freight_cost_api.save_freight_cost(
			transport_type=transport_type, weight_kg=weight_kg, freight_cost=freight_cost, entry_date=entry_date
		)

	def test_reference_is_empty_until_there_are_entries(self):
		self.assertEqual(freight_cost_api.freight_reference()["byType"], [])

	def test_average_is_weighted_by_kg_not_a_mean_of_per_kg_figures(self):
		# 500 kg at ₹5/kg and 20,000 kg at ₹2/kg: weighted = (2500 + 40000) / 20500, not (5 + 2) / 2.
		self._add("Truck", 500, 2500)
		self._add("Truck", 20000, 40000)
		truck = freight_cost_api.freight_reference()["byType"][0]
		self.assertEqual(truck["transportType"], "Truck")
		self.assertEqual(truck["costPerKg"], round(42500 / 20500, 2))
		self.assertEqual(truck["shipments"], 2)
		self.assertEqual(truck["totalKg"], 20500)

	def test_trucks_and_containers_are_averaged_separately(self):
		self._add("Truck", 10000, 30000)
		self._add("Container", 25000, 50000)
		by_type = {r["transportType"]: r["costPerKg"] for r in freight_cost_api.freight_reference()["byType"]}
		self.assertEqual(by_type, {"Truck": 3.0, "Container": 2.0})

	def test_only_the_last_six_months_count(self):
		self._add("Truck", 1000, 9000, entry_date=add_months(today(), -7))
		self._add("Truck", 1000, 3000, entry_date=add_days(today(), -10))
		truck = freight_cost_api.freight_reference()["byType"][0]
		self.assertEqual((truck["costPerKg"], truck["shipments"]), (3.0, 1))

	def test_rejects_a_zero_weight_or_cost_and_an_unknown_transport_type(self):
		with self.assertRaises(frappe.ValidationError):
			self._add("Truck", 0, 100)
		with self.assertRaises(frappe.ValidationError):
			self._add("Truck", 100, 0)
		with self.assertRaises(frappe.ValidationError):
			self._add("Airplane", 100, 100)

	def test_a_dealer_session_cannot_read_or_write_freight_costs(self):
		from dms_erp.auth.dealer_api import _dealer_portal_user
		from dms_erp.warehouse.test_fixtures import ensure_company, make_dealer

		ensure_company()
		frappe.set_user(_dealer_portal_user(make_dealer("Freight Cost Test Dealer")))
		with self.assertRaises(frappe.PermissionError):
			freight_cost_api.freight_reference()
		with self.assertRaises(frappe.PermissionError):
			freight_cost_api.list_freight_costs()
		with self.assertRaises(frappe.PermissionError):
			freight_cost_api.save_freight_cost(transport_type="Truck", weight_kg=1, freight_cost=1)

	def test_list_and_delete(self):
		entry = self._add("Truck", 1000, 3000)
		self.assertEqual(entry["costPerKg"], 3.0)
		self.assertIn(entry["id"], [r["id"] for r in freight_cost_api.list_freight_costs()["items"]])
		freight_cost_api.delete_freight_cost(entry["id"])
		self.assertFalse(frappe.db.exists("Freight Cost Entry", entry["id"]))
