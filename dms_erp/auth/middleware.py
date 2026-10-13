"""before_request hook: resolves our own JWT bearer tokens to frappe.session.user.

This intentionally bypasses Frappe's cookie-based LoginManager entirely — there is no
`sid` cookie in play for staff-app traffic, so CSRF validation (which only triggers for
cookie-backed sessions) never engages either. If no/garbage Authorization header is
present we simply do nothing and leave the request as Guest; frappe.whitelist's own
allow_guest=False check then rejects any protected staff-app endpoint with a clean
PermissionError, so there's no need to raise here.

Because this hook is registered globally (`before_request`, not scoped to this app's
own routes), it runs on *every* request to the site -- including another installed
app's own Bearer-token traffic, e.g. an MCP server app's OAuth-authenticated calls.
A real deploy hit exactly this: `dms_erp_jwt_keys`/`dms_erp_jwt_active_kid` hadn't
been set in site_config.json yet (see README's "Required site_config.json keys" --
deliberately not auto-generated), so jwt_utils.decode_access_token raised
SigningKeyNotConfigured -- a plain Exception, not a jwt.PyJWTError -- for *any* request
bearing an `Authorization: Bearer ...` header, dms_erp's own or not. That propagated
out of this before_request hook uncaught, turning into a 500 on every Bearer-token
request to the whole site (Desk/cookie-based browsing was unaffected, which is why
only the Bearer-token-authenticated MCP connector broke). Caught alongside
PyJWTError below for the same reason as everything else in this function: dms_erp not
being fully configured yet must never take down another app's auth on the same site.

BRD C.13 / Part B.2: "[the middleware] restricts access... enforces dealer-wise
catalog/pricing/eligibility... exposes only required APIs". A dealer-portal account
(role DMS Dealer, no staff role) resolving here is confined to the dealer-portal API
surface below -- everything else in this app (list_inquiries, list_orders, get_dealer,
the whole staff read surface) has never needed a role check on its GET endpoints,
because until dealer accounts existed only staff could ever log in at all. That
assumption breaks the moment a dealer session exists, so this is the one place that
enforces it, rather than auditing and re-gating every read endpoint across the app.
"""

import frappe
import jwt as pyjwt

from dms_erp.auth import jwt_utils
from dms_erp.auth.api import STAFF_ROLES

BEARER_PREFIX = "Bearer "

DEALER_PORTAL_PREFIXES = (
	"dms_erp.auth.dealer_api.",
	"dms_erp.sales.dealer_portal_api.",
)


def _enforce_dealer_scope(request, user: str):
	roles = set(frappe.get_roles(user))
	if "DMS Dealer" not in roles or roles & set(STAFF_ROLES):
		return  # not a dealer-only account -- the ordinary staff role gates apply as normal

	method_path = request.path.removeprefix("/api/method/")
	if not method_path.startswith(DEALER_PORTAL_PREFIXES):
		frappe.throw("This account can only access the dealer portal.", frappe.PermissionError)


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
	except (pyjwt.PyJWTError, jwt_utils.SigningKeyNotConfigured):
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

	# frappe.set_user() resets frappe.local.form_dict as a side effect (it's normally
	# called before the request body/query string has been parsed). This hook runs
	# after that parsing, so the request's args would otherwise be silently wiped out.
	form_dict = frappe.local.form_dict
	frappe.set_user(user)
	frappe.local.form_dict = form_dict

	_enforce_dealer_scope(request, user)
