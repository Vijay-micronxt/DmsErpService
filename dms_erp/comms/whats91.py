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

`receive_webhook` is the inbound counterpart (https://developers.whats91.com/
webhooks/examples): whats91's own event envelope --
`{"event": ..., "data": {...}}` -- and its own auth (a header token, set on
whats91's dashboard against a webhook pointed at this function), neither of
which match comms.api.webhook_inbound_message's generic contract (a flat
`{secret, phone, text}` body). This translates one into the other rather than
teaching the generic endpoint whats91-specific shapes; point whats91's
dashboard "Endpoint URL" at THIS function
(dms_erp.comms.whats91.receive_webhook), not the generic one. Config:

  dms_erp_whats91_webhook_token  -- must match whats91's dashboard
                                     "Verification Token" field exactly
  dms_erp_whats91_webhook_header -- optional, defaults to
                                     "X-Whats91-Webhook-Token" (whats91's own
                                     default "Verification Header Name") --
                                     only set this if you changed that field
                                     on whats91's side too
  dms_erp_whatsapp_webhook_secret -- the same one webhook_inbound_message
                                      already uses; this module supplies it
                                      internally when calling that function,
                                      it's never exposed to whats91 itself
"""

import frappe
import requests

from dms_erp.phone_utils import clean_indian_mobile

WHATS91_SEND_URL = "https://graph.whats91.com/api/v2/send"
_REQUEST_TIMEOUT_SECONDS = 30
WHATS91_WEBHOOK_HEADER_DEFAULT = "X-Whats91-Webhook-Token"


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

	clean_phone = clean_indian_mobile(phone)
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


def _verify_whats91_webhook_token(received_token: str | None):
	"""whats91 authenticates its own webhook deliveries with a request header (its
	dashboard's "Verification Token" + "Verification Header Name" fields) -- a
	different mechanism from comms.api's generic webhook_*'s body-level `secret`
	param, so it gets its own site_config key rather than overloading that one for
	two different checks against two different callers.

	Takes the already-extracted header value rather than reaching into
	frappe.local.request itself, so it's directly unit-testable with a plain string
	-- same pattern auth.middleware._enforce_dealer_scope uses for the same reason."""
	configured = frappe.conf.get("dms_erp_whats91_webhook_token")
	if not configured:
		frappe.throw("dms_erp_whats91_webhook_token is not configured in site_config.json.")
	if not received_token or received_token != configured:
		frappe.throw("Invalid whats91 webhook token.", frappe.PermissionError)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_webhook(event: str | None = None, data: dict | None = None, **kwargs):
	"""whats91's own webhook delivery -- see this module's docstring for the event
	envelope and why this exists separately from comms.api.webhook_inbound_message.

	Acknowledges (200) any event this doesn't act on, rather than throwing --
	whats91 retries failed deliveries (see its dashboard's own "Retry failed
	deliveries" setting), and there's no reason to make it retry forever for an
	event we deliberately don't act on yet. Right now that's everything except
	`message.inbound.text`: the message.status.* delivery-receipt events have
	nothing to reconcile against, because dms_erp doesn't yet record whats91's own
	messageId anywhere when it logs an outbound message (comms.api._send_message
	never actually calls whats91 for a general message, only send_otp_template
	does, and only for the OTP template) -- wiring status receipts up is a
	follow-on once outbound sending for general messages exists.

	Sets frappe.response["success"] directly rather than just returning a value --
	Frappe wraps a whitelisted method's return value under a "message" key
	(`{"message": {"success": true}}`), but whats91's own examples show it checking
	for a literal top-level `success` boolean (its error samples are shaped
	`{"success": false, "message": "...", ...}`), so the wrapped shape alone reads
	as a failure to whats91 even though the call succeeded."""
	header_name = frappe.conf.get("dms_erp_whats91_webhook_header") or WHATS91_WEBHOOK_HEADER_DEFAULT
	received_token = frappe.local.request.headers.get(header_name) if frappe.local.request else None
	_verify_whats91_webhook_token(received_token)

	data = data or {}

	if event == "message.inbound.text":
		phone = data.get("from")
		text = data.get("text")
		if not phone or not text:
			frappe.throw("Malformed message.inbound.text payload -- missing 'from' or 'text'.", frappe.ValidationError)

		from dms_erp.comms.api import webhook_inbound_message

		webhook_inbound_message(
			secret=frappe.conf.get("dms_erp_whatsapp_webhook_secret"),
			phone=phone,
			text=text,
			sent_at=data.get("timestamp"),
		)

	frappe.local.response["success"] = True
	return {"success": True}
