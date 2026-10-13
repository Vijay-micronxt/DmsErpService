"""Dealer-portal auth (BRD C.13) — the "no OTP, reserved for the dealer-facing app"
gap auth/api.py's own docstring already flagged. Phone + OTP is the only way in for
a dealer with no password set yet: request_otp finds the Customer whose
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

Email + password (login_with_password) is a second, faster way in for a dealer who
has set one up -- but there's no email-delivery infrastructure anywhere in this app
(no frappe.sendmail call exists in this codebase), so there is deliberately no
"forgot password" email flow. OTP already fills that role: a dealer
sets their own password from set_my_password once they're signed in (via OTP the
first time, or an already-set password thereafter, same as changing it), and a
dealer who forgets it just falls back to OTP again rather than needing a reset link
that couldn't be delivered anyway. Resolving email -> dealer -> the one portal User
(same account request_otp/verify_otp would resolve to) means Customer.custom_email
must stay unique across dealers once it's used as a login key this way -- see
sales.dealer_api._assert_email_available, the one guard that makes this safe.

All three of request_otp/verify_otp/login_with_password return the same generic
response/error regardless of whether the phone or email is actually registered --
a dealer login surface is the one place in this app an unauthenticated caller can
probe at all, so it must not leak which phone numbers or emails exist.

Not built here: rate-limiting request_otp/login_with_password beyond the one-per-
cooldown-window check on OTP resend below. A dedicated per-identifier/per-IP
limiter is a real follow-up once this ships, not something to improvise without
knowing the actual abuse patterns.
"""

import hashlib
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import check_password, update_password

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


@frappe.whitelist(allow_guest=True, methods=["POST"])
def login_with_password(email: str, password: str, device_id: str, device_name: str | None = None):
	"""Second way in, for a dealer who's already set a password (see set_my_password) --
	resolves Customer.custom_email -> dealer -> the same portal User verify_otp would
	resolve to (never the raw input treated as a Frappe login id, since that account's
	real User.name is a synthetic, never-shown email -- see _dealer_portal_user), then
	verifies the password the ordinary Frappe way (frappe.utils.password.check_password,
	never rolled by hand, same as auth.api.login does for staff)."""
	if not email or not password or not device_id:
		frappe.throw(_("email, password and device_id are required"), frappe.ValidationError)

	generic_error = _("Invalid email or password.")

	dealer = frappe.db.get_value("Customer", {"custom_email": email.strip(), "disabled": 0}, "name")
	if not dealer:
		frappe.throw(generic_error, frappe.AuthenticationError)

	user = frappe.db.get_value("User", {"custom_dealer": dealer}, "name")
	if not user:
		# No portal account exists yet at all (this dealer has never logged in via OTP,
		# so set_my_password was never reachable either) -- same generic error, never
		# reveal that the email itself is otherwise a real, registered dealer.
		frappe.throw(generic_error, frappe.AuthenticationError)

	try:
		check_password(user, password)
	except frappe.AuthenticationError:
		frappe.throw(generic_error, frappe.AuthenticationError)

	if not frappe.db.get_value("User", user, "enabled"):
		frappe.throw(_("This account is disabled."), frappe.AuthenticationError)

	return _issue_tokens(user, device_id, device_name)


@frappe.whitelist(methods=["POST"])
def set_my_password(password: str):
	"""Lets an already-authenticated dealer (signed in via OTP the first time, or an
	already-set password after that) set or change their own portal password, so
	login_with_password has something to check next time. Scoped to
	frappe.session.user, never a caller-supplied dealer id -- a dealer must already
	hold a valid session to set their own password, the same way auth.api.logout_all
	only ever acts on the caller's own sessions. Requires Customer.custom_email to
	already be set (sales.dealer_portal_api.update_my_email) -- a password with no
	email on file would have no way back in via login_with_password at all."""
	dealer = frappe.db.get_value("User", frappe.session.user, "custom_dealer")
	if not dealer:
		frappe.throw(_("This isn't a dealer portal account."), frappe.PermissionError)
	if not frappe.db.get_value("Customer", dealer, "custom_email"):
		frappe.throw(_("Add an email to your profile before setting a password."), frappe.ValidationError)
	if len(password or "") < 8:
		frappe.throw(_("Password must be at least 8 characters."), frappe.ValidationError)

	update_password(frappe.session.user, password)
	return {"success": True}
