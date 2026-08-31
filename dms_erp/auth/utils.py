import hashlib
import secrets

import frappe

# Order matters: this is priority for `primary_role`, highest-privilege first.
APP_ROLE_SLUGS = {
	"DMS Management": "management",
	"DMS Purchase": "purchase",
	"DMS Warehouse": "warehouse",
	"DMS Sales": "sales",
}
APP_ROLE_PRIORITY = list(APP_ROLE_SLUGS.keys())


def generate_opaque_token() -> str:
	return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
	return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_primary_role(user_roles) -> str | None:
	user_roles = set(user_roles)
	for role in APP_ROLE_PRIORITY:
		if role in user_roles:
			return APP_ROLE_SLUGS[role]
	return None


def build_user_profile(user: str) -> dict:
	doc = frappe.get_cached_doc("User", user)
	roles = frappe.get_roles(user)
	app_roles = [APP_ROLE_SLUGS[r] for r in APP_ROLE_PRIORITY if r in roles]
	return {
		"name": doc.name,
		"email": doc.email,
		"full_name": doc.full_name,
		"roles": roles,
		"app_roles": app_roles,
		"primary_role": resolve_primary_role(roles),
	}
