"""Whitelisted staff-app auth endpoints.

Username+password only in this phase (no OTP — that's reserved for the separate
dealer-facing app coming later). Passwords are verified through Frappe's own
frappe.utils.password.check_password, never rolled by hand.

Token model:
- access_token: short-lived JWT (see jwt_utils), stateless-verified per request by
  the before_request middleware, but still checked against Auth Session.revoked_at
  so logout/logout_all take effect immediately rather than waiting for expiry.
- refresh_token: opaque random string, never sent back to us except to this module.
  Only its sha256 hash is ever persisted (Auth Session.refresh_token_hash).

Refresh rotation + reuse detection: every refresh_token call consumes the presented
token and issues a new one (Auth Session.refresh_token_hash is replaced, the just-
consumed hash is kept for one generation in prev_refresh_token_hash). If a refresh
token that matches prev_refresh_token_hash is ever presented again, that can only
mean it was already used once before and is being replayed (e.g. a stolen token) —
so we revoke that session outright instead of issuing new tokens.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import check_password

from dms_erp.auth import jwt_utils
from dms_erp.auth.utils import build_user_profile, generate_opaque_token, hash_token

# System Manager is included as an admin escape hatch (e.g. MicroNXT support access);
# every real staff user should hold one of the four Pacific roles.
STAFF_ROLES = ["Pacific Sales", "Pacific Warehouse", "Pacific Purchase", "Pacific Management", "System Manager"]


def _assert_staff_access(user: str):
	if not set(frappe.get_roles(user)) & set(STAFF_ROLES):
		frappe.throw(_("This account is not enabled for the staff app."), frappe.PermissionError)


def _issue_tokens(user: str, device_id: str, device_name: str | None) -> dict:
	refresh_token = generate_opaque_token()
	issued_at = now_datetime()
	expires_at = add_to_date(issued_at, days=jwt_utils.refresh_token_ttl_days())

	session = frappe.get_doc(
		{
			"doctype": "Auth Session",
			"user": user,
			"device_id": device_id,
			"device_name": device_name,
			"refresh_token_hash": hash_token(refresh_token),
			"issued_at": issued_at,
			"expires_at": expires_at,
		}
	)
	session.insert(ignore_permissions=True)

	access_token, expires_in = jwt_utils.encode_access_token(user, session.name)

	return {
		"access_token": access_token,
		"refresh_token": refresh_token,
		"token_type": "Bearer",
		"expires_in": expires_in,
		"user": build_user_profile(user),
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def login(usr: str, pwd: str, device_id: str, device_name: str | None = None):
	if not usr or not pwd or not device_id:
		frappe.throw(_("usr, pwd and device_id are required"), frappe.ValidationError)

	user = check_password(usr, pwd)  # raises frappe.AuthenticationError on bad creds

	if not frappe.db.get_value("User", user, "enabled"):
		frappe.throw(_("This account is disabled"), frappe.AuthenticationError)

	_assert_staff_access(user)

	return _issue_tokens(user, device_id, device_name)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def refresh_token(refresh_token: str):
	if not refresh_token:
		frappe.throw(_("refresh_token is required"), frappe.ValidationError)

	presented_hash = hash_token(refresh_token)
	now = now_datetime()

	session_name = frappe.db.get_value(
		"Auth Session",
		{"refresh_token_hash": presented_hash, "revoked_at": ["is", "not set"]},
		"name",
	)

	if not session_name:
		reused_session_name = frappe.db.get_value(
			"Auth Session",
			{"prev_refresh_token_hash": presented_hash, "revoked_at": ["is", "not set"]},
			"name",
		)
		if reused_session_name:
			frappe.db.set_value("Auth Session", reused_session_name, "revoked_at", now)
			frappe.throw(
				_("This refresh token has already been used. The session has been revoked for security."),
				frappe.AuthenticationError,
			)
		frappe.throw(_("Invalid refresh token"), frappe.AuthenticationError)

	session = frappe.get_doc("Auth Session", session_name)

	if session.expires_at and session.expires_at < now:
		session.revoked_at = now
		session.save(ignore_permissions=True)
		frappe.throw(_("Session expired, please log in again"), frappe.AuthenticationError)

	if not frappe.db.get_value("User", session.user, "enabled"):
		session.revoked_at = now
		session.save(ignore_permissions=True)
		frappe.throw(_("This account is disabled"), frappe.AuthenticationError)

	new_refresh_token = generate_opaque_token()
	session.prev_refresh_token_hash = session.refresh_token_hash
	session.refresh_token_hash = hash_token(new_refresh_token)
	session.issued_at = now
	session.expires_at = add_to_date(now, days=jwt_utils.refresh_token_ttl_days())
	session.save(ignore_permissions=True)

	access_token, expires_in = jwt_utils.encode_access_token(session.user, session.name)

	return {
		"access_token": access_token,
		"refresh_token": new_refresh_token,
		"token_type": "Bearer",
		"expires_in": expires_in,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def logout(refresh_token: str):
	if not refresh_token:
		frappe.throw(_("refresh_token is required"), frappe.ValidationError)

	presented_hash = hash_token(refresh_token)
	session_name = frappe.db.get_value(
		"Auth Session",
		{"refresh_token_hash": presented_hash, "revoked_at": ["is", "not set"]},
		"name",
	)
	if session_name:
		frappe.db.set_value("Auth Session", session_name, "revoked_at", now_datetime())

	# Idempotent either way — don't leak whether the token was recognised.
	return {"success": True}


@frappe.whitelist(methods=["POST"])
def logout_all():
	frappe.db.set_value(
		"Auth Session",
		{"user": frappe.session.user, "revoked_at": ["is", "not set"]},
		"revoked_at",
		now_datetime(),
	)
	return {"success": True}


@frappe.whitelist(methods=["GET"])
def me():
	return build_user_profile(frappe.session.user)
