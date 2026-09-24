"""Real WhatsApp delivery for the dealer-portal OTP, via whats91's Meta-channel
template send (https://graph.whats91.com/api/v2/send). This is intentionally
narrow -- it sends exactly one thing: a Meta Authentication-category template
carrying the OTP code, with a Copy Code button whose payload is the same code.
It is not a general messaging transport; comms.api._send_message/webhook_* stay
the audit-log system of record for every WhatsApp message (per that module's own
docstring) regardless of whether this actually delivers anything -- this module
is called *in addition to* that log write, not instead of it.

Deliberately independent of the erpnext_enhancements app's own (more general)
whats91 integration and its WhatsApp Settings doctype -- dms_erp must keep
working on any site whether or not that app happens to be installed, so its own
credentials live in site_config.json instead:

  dms_erp_whats91_auth_token   -- the whats91 Meta-channel auth token
  dms_erp_whats91_otp_template -- the Meta-approved Authentication template name
                                   (single {{1}} variable: the OTP code)

Neither configured -> send_otp_template logs a warning and returns False; it
never raises, since request_otp's own generic response must not change shape
just because WhatsApp delivery isn't set up on a given site yet.
"""

import re

import frappe
import requests

WHATS91_SEND_URL = "https://graph.whats91.com/api/v2/send"
_REQUEST_TIMEOUT_SECONDS = 30


def _clean_phone_number(phone: str) -> str | None:
	digits = re.sub(r"\D", "", phone or "")
	if digits.startswith("91") and len(digits) == 12:
		digits = digits[2:]
	if len(digits) == 10 and digits[0] in "6789":
		return digits
	return None


def _is_send_successful(response) -> bool:
	"""whats91 returns the literal word "success" in every response body,
	failures included ({"success": false, ...}) -- the flag has to be read from
	parsed JSON, a substring match on the raw text would treat every error as
	a successful send."""
	if response.status_code != 200:
		return False

	try:
		data = response.json()
	except ValueError:
		return False

	if isinstance(data, list):
		data = data[0] if data else {}
	if not isinstance(data, dict):
		return False

	if "success" in data:
		return bool(data["success"])
	if data.get("error"):
		return False
	if data.get("messages"):
		return True

	return data.get("status") in ("success", "accepted") or data.get("result") == "success"


def send_otp_template(phone: str, otp_code: str) -> bool:
	"""Sends the approved Authentication template with `otp_code` as its one
	body variable and as the Copy Code button's payload. Returns False (never
	raises) on any failure -- missing config, an invalid phone, a network error,
	or whats91 reporting the send itself failed -- so a WhatsApp outage never
	breaks request_otp's own response."""
	auth_token = frappe.conf.get("dms_erp_whats91_auth_token")
	template_name = frappe.conf.get("dms_erp_whats91_otp_template")
	if not auth_token or not template_name:
		frappe.logger().warning(
			"whats91 not configured (dms_erp_whats91_auth_token / dms_erp_whats91_otp_template) "
			"-- dealer OTP was not sent over WhatsApp."
		)
		return False

	clean_phone = _clean_phone_number(phone)
	if not clean_phone:
		frappe.logger().error(f"whats91 OTP send skipped -- invalid phone number: {phone!r}")
		return False

	payload = {
		"authToken": auth_token,
		"receiverId": f"91{clean_phone}",
		"templateName": template_name,
		"parameters": [otp_code],
		"buttonParameters": [otp_code],
	}

	try:
		response = requests.post(
			WHATS91_SEND_URL, json=payload, headers={"Content-Type": "application/json"}, timeout=_REQUEST_TIMEOUT_SECONDS
		)
	except requests.RequestException as e:
		frappe.logger().error(f"whats91 OTP send failed (request error): {e}")
		return False

	if not _is_send_successful(response):
		frappe.logger().error(f"whats91 OTP send failed: HTTP {response.status_code} | {response.text[:500]}")
		return False

	frappe.logger().info(f"whats91 OTP template '{template_name}' sent to {clean_phone}")
	return True
