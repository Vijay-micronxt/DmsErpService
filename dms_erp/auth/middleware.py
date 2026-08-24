"""before_request hook: resolves our own JWT bearer tokens to frappe.session.user.

This intentionally bypasses Frappe's cookie-based LoginManager entirely — there is no
`sid` cookie in play for staff-app traffic, so CSRF validation (which only triggers for
cookie-backed sessions) never engages either. If no/garbage Authorization header is
present we simply do nothing and leave the request as Guest; frappe.whitelist's own
allow_guest=False check then rejects any protected staff-app endpoint with a clean
PermissionError, so there's no need to raise here.
"""

import frappe
import jwt as pyjwt

from dms_erp.auth import jwt_utils

BEARER_PREFIX = "Bearer "


def authenticate_request():
	request = frappe.local.request
	if not request:
		return

	auth_header = request.headers.get("Authorization")
	if not auth_header or not auth_header.startswith(BEARER_PREFIX):
		return

	token = auth_header[len(BEARER_PREFIX):].strip()
	if not token:
		return

	try:
		payload = jwt_utils.decode_access_token(token)
	except pyjwt.PyJWTError:
		return

	user = payload.get("sub")
	session_name = payload.get("sid")
	if not user or not session_name:
		return

	session = frappe.db.get_value(
		"Auth Session", session_name, ["user", "revoked_at"], as_dict=True
	)
	if not session or session.user != user or session.revoked_at:
		return

	frappe.set_user(user)
