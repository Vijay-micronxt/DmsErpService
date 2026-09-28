import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.purchase import transporter_api


class TestTransporterApi(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all("Transporter", filters={"transporter_name": ["like", "Test Transporter%"]}, pluck="name"):
			frappe.delete_doc("Transporter", name, force=True, ignore_permissions=True)

	def _vehicle(self, **overrides):
		vehicle = {
			"owner_name": "Ramesh Patel",
			"vehicle_number": "gj-05-kl-2210",
			"vehicle_type": "Truck",
			"mode": "Road",
			"capacity_tonnes": 25,
			"transit_days": 6,
			"standard_rate_basis": 45000,
			"driver_name": "Suresh",
			"driver_mobile": "9620204657",
		}
		vehicle.update(overrides)
		return vehicle

	def test_create_transporter_with_no_vehicles(self):
		t = transporter_api.create_transporter(transporter_name="Test Transporter A")
		self.assertEqual(t["transporterName"], "Test Transporter A")
		self.assertEqual(t["vehicles"], [])

	def test_create_transporter_with_multiple_vehicles(self):
		t = transporter_api.create_transporter(
			transporter_name="Test Transporter B",
			contact="9876543210",
			vehicles=[
				self._vehicle(vehicle_number="GJ-05-KL-1001"),
				self._vehicle(vehicle_number="GJ-05-KL-1002", vehicle_type="Container", mode="Rail + Road", capacity_tonnes=32.5),
			],
		)
		self.assertEqual(len(t["vehicles"]), 2)
		self.assertEqual(t["vehicles"][0]["vehicleNumber"], "GJ-05-KL-1001")
		self.assertEqual(t["vehicles"][1]["vehicleType"], "Container")

	def test_vehicle_number_is_normalized_to_uppercase(self):
		t = transporter_api.create_transporter(
			transporter_name="Test Transporter C", vehicles=[self._vehicle(vehicle_number="  gj-05-kl-2210  ")]
		)
		self.assertEqual(t["vehicles"][0]["vehicleNumber"], "GJ-05-KL-2210")

	def test_vehicle_number_required(self):
		with self.assertRaises(frappe.ValidationError):
			transporter_api.create_transporter(transporter_name="Test Transporter D", vehicles=[self._vehicle(vehicle_number="")])

	def test_invalid_capacity_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			transporter_api.create_transporter(transporter_name="Test Transporter E", vehicles=[self._vehicle(capacity_tonnes=0)])

	def test_invalid_mode_rejected(self):
		# mode is a Select field -- Frappe's own doc.insert() validation rejects an
		# option outside the field's configured list, same as every other Select
		# field in this app (never hand-revalidated in the API layer).
		with self.assertRaises(frappe.ValidationError):
			transporter_api.create_transporter(transporter_name="Test Transporter F", vehicles=[self._vehicle(mode="Sea")])

	def test_duplicate_vehicle_number_within_same_request_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			transporter_api.create_transporter(
				transporter_name="Test Transporter G",
				vehicles=[self._vehicle(vehicle_number="GJ-05-KL-9999"), self._vehicle(vehicle_number="gj-05-kl-9999")],
			)

	def test_duplicate_vehicle_number_across_transporters_rejected(self):
		transporter_api.create_transporter(transporter_name="Test Transporter H1", vehicles=[self._vehicle(vehicle_number="GJ-05-KL-5555")])
		with self.assertRaises(frappe.ValidationError):
			transporter_api.create_transporter(transporter_name="Test Transporter H2", vehicles=[self._vehicle(vehicle_number="GJ-05-KL-5555")])

	def test_invalid_driver_mobile_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			transporter_api.create_transporter(transporter_name="Test Transporter I", vehicles=[self._vehicle(driver_mobile="12345")])

	def test_driver_mobile_optional(self):
		t = transporter_api.create_transporter(
			transporter_name="Test Transporter J", vehicles=[self._vehicle(driver_name=None, driver_mobile=None)]
		)
		self.assertIsNone(t["vehicles"][0]["driverMobile"])

	def test_add_vehicle_to_existing_transporter(self):
		t = transporter_api.create_transporter(transporter_name="Test Transporter K")
		updated = transporter_api.add_vehicle(t["id"], self._vehicle(vehicle_number="GJ-05-KL-3003"))
		self.assertEqual(len(updated["vehicles"]), 1)

	def test_update_vehicle(self):
		t = transporter_api.create_transporter(transporter_name="Test Transporter L", vehicles=[self._vehicle(vehicle_number="GJ-05-KL-4004")])
		vehicle_id = t["vehicles"][0]["id"]
		updated = transporter_api.update_vehicle(t["id"], vehicle_id, {"capacity_tonnes": 31})
		self.assertEqual(updated["vehicles"][0]["capacityTonnes"], 31)
		# Updating one field shouldn't drop the others already on the row.
		self.assertEqual(updated["vehicles"][0]["vehicleNumber"], "GJ-05-KL-4004")

	def test_update_vehicle_keeping_its_own_number_does_not_self_collide(self):
		t = transporter_api.create_transporter(transporter_name="Test Transporter M", vehicles=[self._vehicle(vehicle_number="GJ-05-KL-6006")])
		vehicle_id = t["vehicles"][0]["id"]
		updated = transporter_api.update_vehicle(t["id"], vehicle_id, {"transit_days": 5})
		self.assertEqual(updated["vehicles"][0]["vehicleNumber"], "GJ-05-KL-6006")

	def test_remove_vehicle(self):
		t = transporter_api.create_transporter(
			transporter_name="Test Transporter N",
			vehicles=[self._vehicle(vehicle_number="GJ-05-KL-7007"), self._vehicle(vehicle_number="GJ-05-KL-7008")],
		)
		vehicle_id = t["vehicles"][0]["id"]
		updated = transporter_api.remove_vehicle(t["id"], vehicle_id)
		self.assertEqual(len(updated["vehicles"]), 1)
		self.assertEqual(updated["vehicles"][0]["vehicleNumber"], "GJ-05-KL-7008")

	def test_list_transporters_is_paginated_and_searchable(self):
		transporter_api.create_transporter(transporter_name="Test Transporter O")
		res = transporter_api.list_transporters(search="Test Transporter O")
		self.assertEqual(res["total"], 1)
		self.assertEqual(res["items"][0]["transporterName"], "Test Transporter O")

	def test_write_requires_purchase_or_system_manager_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			transporter_api.create_transporter(transporter_name="Test Transporter P")
