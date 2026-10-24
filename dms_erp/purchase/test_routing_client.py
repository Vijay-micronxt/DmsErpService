from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.purchase import routing_client

TEST_API_KEY = "unit-test-maps-key"


class _FakeResponse:
	def __init__(self, status_code=200, json_body=None, text=""):
		self.status_code = status_code
		self._json_body = json_body
		self.text = text or (str(json_body) if json_body is not None else "")

	def json(self):
		if self._json_body is None:
			raise ValueError("no json")
		return self._json_body


_ORIGIN = (12.9716, 77.5946)
_STOP_A = (22.8181, 70.8328)
_STOP_B = (22.4707, 70.0577)


def _route_response(order, legs, polyline=None):
	route = {
		"optimizedIntermediateWaypointIndex": order,
		"legs": [{"distanceMeters": leg[0] * 1000, "duration": f"{leg[1] * 60}s"} for leg in legs],
	}
	if polyline is not None:
		route["polyline"] = {"encodedPolyline": polyline}
	return {"routes": [route]}


class TestRoutingClient(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.conf.dms_erp_google_maps_api_key = TEST_API_KEY

	@classmethod
	def tearDownClass(cls):
		frappe.conf.pop("dms_erp_google_maps_api_key", None)
		super().tearDownClass()

	def test_unconfigured_key_raises_without_calling_the_api(self):
		frappe.conf.pop("dms_erp_google_maps_api_key", None)
		try:
			with patch("dms_erp.purchase.routing_client.requests.post") as mock_post:
				with self.assertRaises(routing_client.RouteOptimizationFailed):
					routing_client.optimize_waypoints(_ORIGIN, [_STOP_A])
				mock_post.assert_not_called()
		finally:
			frappe.conf.dms_erp_google_maps_api_key = TEST_API_KEY

	def test_optimize_waypoints_sends_the_expected_payload(self):
		with patch("dms_erp.purchase.routing_client.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(
				200, _route_response([1, 0], [[100, 120], [80, 90]], polyline="abc123")
			)
			result = routing_client.optimize_waypoints(_ORIGIN, [_STOP_A, _STOP_B])

		mock_post.assert_called_once()
		_args, kwargs = mock_post.call_args
		self.assertEqual(kwargs["headers"]["X-Goog-Api-Key"], TEST_API_KEY)
		self.assertIn("routes.polyline.encodedPolyline", kwargs["headers"]["X-Goog-FieldMask"])
		self.assertTrue(kwargs["json"]["optimizeWaypointOrder"])
		self.assertEqual(len(kwargs["json"]["intermediates"]), 2)
		self.assertEqual(result["order"], [1, 0])
		self.assertEqual(result["legs"], [{"distance_km": 100.0, "duration_minutes": 120}, {"distance_km": 80.0, "duration_minutes": 90}])
		self.assertEqual(result["polyline"], "abc123")

	def test_optimize_waypoints_polyline_is_none_when_google_omits_it(self):
		with patch("dms_erp.purchase.routing_client.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(200, _route_response([0], [[10, 10]]))
			result = routing_client.optimize_waypoints(_ORIGIN, [_STOP_A])

		self.assertIsNone(result["polyline"])

	def test_compute_fixed_order_legs_does_not_ask_for_optimization(self):
		with patch("dms_erp.purchase.routing_client.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(200, _route_response(None, [[50, 60]], polyline="xyz789"))
			result = routing_client.compute_fixed_order_legs(_ORIGIN, [_STOP_A])

		_args, kwargs = mock_post.call_args
		self.assertFalse(kwargs["json"]["optimizeWaypointOrder"])
		self.assertEqual(result["legs"], [{"distance_km": 50.0, "duration_minutes": 60}])
		self.assertEqual(result["polyline"], "xyz789")

	def test_compute_fixed_order_legs_returns_empty_without_a_network_call(self):
		with patch("dms_erp.purchase.routing_client.requests.post") as mock_post:
			result = routing_client.compute_fixed_order_legs(_ORIGIN, [])
			mock_post.assert_not_called()
		self.assertEqual(result, {"legs": [], "polyline": None})

	def test_a_non_200_response_raises_route_optimization_failed(self):
		with patch("dms_erp.purchase.routing_client.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(403, {"error": "forbidden"})
			with self.assertRaises(routing_client.RouteOptimizationFailed):
				routing_client.optimize_waypoints(_ORIGIN, [_STOP_A])

	def test_no_routes_in_the_response_raises_route_optimization_failed(self):
		with patch("dms_erp.purchase.routing_client.requests.post") as mock_post:
			mock_post.return_value = _FakeResponse(200, {"routes": []})
			with self.assertRaises(routing_client.RouteOptimizationFailed):
				routing_client.optimize_waypoints(_ORIGIN, [_STOP_A])

	def test_a_network_error_raises_route_optimization_failed(self):
		import requests

		with patch("dms_erp.purchase.routing_client.requests.post") as mock_post:
			mock_post.side_effect = requests.RequestException("boom")
			with self.assertRaises(routing_client.RouteOptimizationFailed):
				routing_client.optimize_waypoints(_ORIGIN, [_STOP_A])
