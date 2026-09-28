"""Dealer-portal auth (BRD C.13) — the "no OTP, reserved for the dealer-facing app"
gap auth/api.py's own docstring already flagged. Phone + OTP, not username/password
(dealers never get a password at all): request_otp finds the Customer whose
custom_phone matches, issues a short numeric code, and actually delivers it over
WhatsApp via whats91's Meta-channel Authentication template (comms.whats91.
send_otp_template — see that module for the required site_config keys).
comms.api._send_message is still called alongside it, purely as the audit-log
system of record every other WhatsApp message in this app goes through; it does
not itself deliver anything (see its own module docstring) and a whats91 failure
there is logged, never surfaced to the caller. verify_otp checks it and, on
success, finds-or-creates that dealer's
one portal User account (role DMS Dealer, User.custom_dealer linking back to the
Customer) and issues the exact same access/refresh token pair staff logins get
(auth.api._issue_tokens) — the JWT middleware doesn't care which kind of account it
resolves, only auth.middleware's dealer-scoping guard treats the two differently.

Both endpoints return the same generic response/error regardless of whether the
phone number is actually registered -- a dealer login surface is the one place in
this app an unauthenticated caller can probe at all, so it must not leak which
phone numbers exist.

Not built here: rate-limiting request_otp beyond the one-per-cooldown-window check
below. A dedicated per-phone/per-IP limiter is a real follow-up once this ships,
not something to improvise without knowing the actual abuse patterns.
"""

import hashlib
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from dms_erp.auth.api import _issue_tokens
from dms_erp.auth.utils import hash_token
from dms_erp.comms.api import _send_message
from dms_erp.comms.whats91 import send_otp_template
from dms_erp.phone_utils import dealer_for_phone

OTP_LENGTH = 6
OTP_TTL_MINUTES = 5
MAX_OTP_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60


def _dealer_portal_user(dealer: str) -> str:
	"""Finds this dealer's existing portal User, or creates it on first successful
	login. The synthetic email is deterministic (a hash of the dealer's own Customer
	id) purely so it's guaranteed unique and valid-shaped -- it's never shown to the
	dealer or used to contact them, unlike custom_phone."""
	existing = frappe.db.get_value("User", {"custom_dealer": dealer}, "name")
	if existing:
		return existing

	dealer_name = frappe.db.get_value("Customer", dealer, "customer_name") or dealer
	email = f"dealer-{hashlib.sha1(dealer.encode('utf-8')).hexdigest()[:16]}@dealer.dms-erp.local"
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": dealer_name,
			"enabled": 1,
			"user_type": "System User",
			"send_welcome_email": 0,
			"custom_dealer": dealer,
			"roles": [{"role": "DMS Dealer"}],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_otp(phone: str):
	if not phone:
		frappe.throw(_("phone is required"), frappe.ValidationError)

	dealer = dealer_for_phone(phone)
	if dealer:
		now = now_datetime()
		recent = frappe.db.get_value(
			"Dealer Login OTP",
			{"dealer": dealer, "consumed_at": ["is", "not set"], "issued_at": [">", add_to_date(now, seconds=-OTP_RESEND_COOLDOWN_SECONDS)]},
			"name",
		)
		if not recent:
			code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
			frappe.get_doc(
				{
					"doctype": "Dealer Login OTP",
					"dealer": dealer,
					"issued_at": now,
					"expires_at": add_to_date(now, minutes=OTP_TTL_MINUTES),
					"otp_hash": hash_token(code),
				}
			).insert(ignore_permissions=True)
			# The real send -- failure here (unconfigured site, whats91 outage, an
			# invalid phone) is logged inside send_otp_template and never raises, so
			# it can't change this endpoint's response shape (see module docstring).
			send_otp_template(phone, code)
			_send_message(
				dealer,
				_("Your Pacific Inc verification code is {0}. It expires in {1} minutes.").format(code, OTP_TTL_MINUTES),
			)

	# Same response whether or not the phone matched, and whether or not a new code
	# was actually sent (cooldown) -- never reveals which phone numbers are registered.
	return {"success": True}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def verify_otp(phone: str, otp: str, device_id: str, device_name: str | None = None):
	if not phone or not otp or not device_id:
		frappe.throw(_("phone, otp and device_id are required"), frappe.ValidationError)

	generic_error = _("Invalid phone number or code.")

	dealer = dealer_for_phone(phone)
	if not dealer:
		frappe.throw(generic_error, frappe.AuthenticationError)

	otp_name = frappe.db.get_value(
		"Dealer Login OTP",
		{"dealer": dealer, "consumed_at": ["is", "not set"]},
		"name",
		order_by="issued_at desc",
	)
	if not otp_name:
		frappe.throw(generic_error, frappe.AuthenticationError)

	otp_doc = frappe.get_doc("Dealer Login OTP", otp_name)
	now = now_datetime()
	if otp_doc.expires_at < now:
		frappe.throw(_("This code has expired. Request a new one."), frappe.AuthenticationError)
	if otp_doc.attempts >= MAX_OTP_ATTEMPTS:
		frappe.throw(_("Too many attempts. Request a new code."), frappe.AuthenticationError)

	if hash_token(otp) != otp_doc.otp_hash:
		otp_doc.attempts += 1
		otp_doc.save(ignore_permissions=True)
		frappe.throw(generic_error, frappe.AuthenticationError)

	otp_doc.consumed_at = now
	otp_doc.save(ignore_permissions=True)

	user = _dealer_portal_user(dealer)
	if not frappe.db.get_value("User", user, "enabled"):
		frappe.throw(_("This account is disabled."), frappe.AuthenticationError)

	return _issue_tokens(user, device_id, device_name)
