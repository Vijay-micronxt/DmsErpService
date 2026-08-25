"""JWT access-token issuance/verification for the staff app.

Refresh tokens are NOT JWTs — they're opaque random strings whose sha256 hash is
stored on Auth Session (see auth/utils.py + auth/api.py). Only access tokens are
JWTs, since they're short-lived and we want stateless verification on every request.

Signing keys are rotatable: site_config.json holds a {kid: secret} map plus the
currently-active kid. To rotate, add a new kid as active and keep the old kid (and
its secret) around in the map until every access token signed with it has expired
(<= dms_erp_access_token_ttl seconds), then remove it.
"""

import time

import frappe
import jwt as pyjwt

JWT_ALGO = "HS256"
DEFAULT_ACCESS_TTL_SECONDS = 40 * 60  # 40 minutes, within the 30-60 min spec range
DEFAULT_REFRESH_TTL_DAYS = 30


class SigningKeyNotConfigured(Exception):
	pass


def _get_keys():
	keys = frappe.conf.get("dms_erp_jwt_keys")
	active_kid = frappe.conf.get("dms_erp_jwt_active_kid")
	if not keys or not active_kid or active_kid not in keys:
		frappe.throw(
			"JWT signing keys are not configured. Set 'dms_erp_jwt_keys' (an object mapping "
			"key-id -> secret) and 'dms_erp_jwt_active_kid' (which key-id to sign new tokens "
			"with) in site_config.json.",
			exc=SigningKeyNotConfigured,
		)
	return keys, active_kid


def access_token_ttl() -> int:
	return frappe.conf.get("dms_erp_access_token_ttl") or DEFAULT_ACCESS_TTL_SECONDS


def refresh_token_ttl_days() -> int:
	return frappe.conf.get("dms_erp_refresh_token_ttl_days") or DEFAULT_REFRESH_TTL_DAYS


def encode_access_token(user: str, session_name: str) -> tuple[str, int]:
	keys, active_kid = _get_keys()
	ttl = access_token_ttl()
	now = int(time.time())
	payload = {
		"sub": user,
		"sid": session_name,
		"type": "access",
		"iat": now,
		"exp": now + ttl,
	}
	token = pyjwt.encode(payload, keys[active_kid], algorithm=JWT_ALGO, headers={"kid": active_kid})
	return token, ttl


def decode_access_token(token: str) -> dict:
	keys, _ = _get_keys()

	try:
		header = pyjwt.get_unverified_header(token)
	except pyjwt.PyJWTError as e:
		raise pyjwt.InvalidTokenError("Malformed token") from e

	secret = keys.get(header.get("kid"))
	if not secret:
		raise pyjwt.InvalidTokenError("Unknown signing key")

	payload = pyjwt.decode(token, secret, algorithms=[JWT_ALGO])
	if payload.get("type") != "access":
		raise pyjwt.InvalidTokenError("Not an access token")
	return payload
