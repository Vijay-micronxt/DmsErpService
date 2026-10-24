"""Google Maps Routes API client for BRD C.5 route planning. Named for the
capability (routing), not the vendor, so the provider stays swappable later
without a rename -- route_plan_api.py never imports `requests` or touches an
HTTP response directly, only this module's two functions.

Unlike comms/whats91.py's send_otp_template (which never raises, since a
silent WhatsApp-delivery failure must not change request_otp's own response
shape), both functions here RAISE RouteOptimizationFailed on any failure --
missing API key, a non-2xx response, a Maps-reported failure status, or a
network error. optimize_route/resequence_route_stops are synchronous staff
button-clicks with no safe default route to fall back to, so the failure
must surface to the caller, not degrade silently.

Config (site_config.json -- a credential, not business data, so it does NOT
live on DMS Purchase Settings alongside the route origin lat/long -- see
README.md's "Required site_config.json keys"):

  dms_erp_google_maps_api_key -- a Google Maps Platform API key with the
                                   Routes API enabled

`optimize_waypoints` lets Google choose the stop order (Routes API's
`optimizeWaypointOrder`); `compute_fixed_order_legs` instead computes legs
for a caller-supplied fixed order (the Directions API) -- used by
resequence_route_stops after a staff member manually re-orders stops, where
the order is no longer Google's to decide.

Both also return the route's `polyline` -- Google's encoded-polyline string
for the real road path (not just the stop points), per BRD C.5's "GPS
coordinates ... plan pickups" -- so the frontend can draw the actual route
instead of a straight line between stops. Only ever stored as the opaque
encoded string; decoding it is the frontend's job (dms_erp has no reason to
decode geometry it never otherwise touches)."""

import frappe
import requests

_ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_REQUEST_TIMEOUT_SECONDS = 30


class RouteOptimizationFailed(Exception):
	pass


def _get_api_key() -> str:
	key = frappe.conf.get("dms_erp_google_maps_api_key")
	if not key:
		frappe.throw(
			"Google Maps API key is not configured. Set 'dms_erp_google_maps_api_key' "
			"in site_config.json (a Google Maps Platform key with the Routes API enabled).",
			exc=RouteOptimizationFailed,
		)
	return key


def _waypoint(point: tuple[float, float]) -> dict:
	lat, lng = point
	return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}


def _call_routes_api(payload: dict, field_mask: str) -> dict:
	api_key = _get_api_key()
	try:
		response = requests.post(
			_ROUTES_API_URL,
			json=payload,
			headers={
				"Content-Type": "application/json",
				"X-Goog-Api-Key": api_key,
				"X-Goog-FieldMask": field_mask,
			},
			timeout=_REQUEST_TIMEOUT_SECONDS,
		)
	except requests.RequestException as e:
		frappe.throw(f"Route computation request failed: {e}", exc=RouteOptimizationFailed)

	if response.status_code != 200:
		frappe.throw(
			f"Route computation failed: HTTP {response.status_code} | {response.text[:500]}",
			exc=RouteOptimizationFailed,
		)

	try:
		data = response.json()
	except ValueError:
		frappe.throw("Route computation returned an invalid response.", exc=RouteOptimizationFailed)

	if not data.get("routes"):
		frappe.throw("Route computation returned no route.", exc=RouteOptimizationFailed)

	return data


def optimize_waypoints(origin: tuple[float, float], stops: list[tuple[float, float]]) -> dict:
	"""Lets Google choose the visiting order for `stops` (Routes API
	`optimizeWaypointOrder: true`), starting and ending at `origin` (the depot).
	Returns {"order": [int, ...], "legs": [{"distance_km": float, "duration_minutes": int}, ...],
	"polyline": str | None} -- `order` is the 0-based permutation of the input
	`stops` list Google chose, `legs` has one entry per stop in that chosen
	order (the leg arriving at it), and `polyline` is the whole route's
	encoded-polyline string (None if Google's response didn't include one)."""
	if not stops:
		frappe.throw("optimize_waypoints needs at least one stop.", exc=RouteOptimizationFailed)

	payload = {
		"origin": _waypoint(origin),
		"destination": _waypoint(origin),
		"intermediates": [_waypoint(s) for s in stops],
		"travelMode": "DRIVE",
		"optimizeWaypointOrder": True,
	}
	data = _call_routes_api(
		payload,
		field_mask="routes.optimizedIntermediateWaypointIndex,routes.legs.distanceMeters,"
		"routes.legs.duration,routes.polyline.encodedPolyline",
	)
	route = data["routes"][0]
	order = route.get("optimizedIntermediateWaypointIndex") or list(range(len(stops)))
	legs = route.get("legs") or []
	# The API returns one leg per hop including the final return-to-origin leg;
	# only the legs arriving at each stop (not the trip back) matter here.
	leg_summaries = [_summarize_leg(leg) for leg in legs[: len(stops)]]
	return {"order": order, "legs": leg_summaries, "polyline": _route_polyline(route)}


def compute_fixed_order_legs(origin: tuple[float, float], ordered_stops: list[tuple[float, float]]) -> dict:
	"""Computes leg distance/duration for a caller-fixed stop order (the
	Directions API, via Routes API's computeRoutes without optimizeWaypointOrder)
	-- used after a staff member manually reorders stops, where Google is no
	longer choosing the sequence, only measuring it. Returns
	{"legs": [{"distance_km", "duration_minutes"}, ...], "polyline": str | None},
	one leg per stop in the given order."""
	if not ordered_stops:
		return {"legs": [], "polyline": None}

	payload = {
		"origin": _waypoint(origin),
		"destination": _waypoint(origin),
		"intermediates": [_waypoint(s) for s in ordered_stops],
		"travelMode": "DRIVE",
		"optimizeWaypointOrder": False,
	}
	data = _call_routes_api(
		payload, field_mask="routes.legs.distanceMeters,routes.legs.duration,routes.polyline.encodedPolyline"
	)
	route = data["routes"][0]
	legs = route.get("legs") or []
	return {"legs": [_summarize_leg(leg) for leg in legs[: len(ordered_stops)]], "polyline": _route_polyline(route)}


def _route_polyline(route: dict) -> str | None:
	return (route.get("polyline") or {}).get("encodedPolyline")


def _summarize_leg(leg: dict) -> dict:
	distance_km = (leg.get("distanceMeters") or 0) / 1000
	duration_seconds = int((leg.get("duration") or "0s").rstrip("s") or 0)
	return {"distance_km": round(distance_km, 2), "duration_minutes": round(duration_seconds / 60)}
