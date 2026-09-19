"""Staff user management — list / create / update over the native `User` doctype.

Only the four DMS roles can be granted or revoked here (DMS Sales / Warehouse / Purchase /
Management, see auth.utils.APP_ROLE_SLUGS). `System Manager` and every other ERPNext role
are never assignable through this API, so a Management user can't hand out admin rights.
Other roles a user already holds are left untouched by `update_user`.

Creating/editing users is Management/System Manager only. `list_users` is open to any staff
user because dropdowns (picker, salesperson, assigned-to) need it — it returns only id,
name, enabled state and the DMS role slugs, never the full ERPNext role list.

Disabling a user also revokes their open Auth Sessions, so a refresh token can't outlive it.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from dms_erp.auth.api import STAFF_ROLES
from dms_erp.auth.utils import APP_ROLE_SLUGS, resolve_app_roles
from dms_erp.pagination import clamp

USER_ADMIN_ROLES = {"DMS Management", "System Manager"}
ASSIGNABLE_ROLES = list(APP_ROLE_SLUGS)
SYSTEM_USERS = ["Administrator", "Guest"]


def _assert_staff():
	if not set(frappe.get_roles(frappe.session.user)) & set(STAFF_ROLES):
		frappe.throw(_("Only staff can list users."), frappe.PermissionError)


def _assert_can_manage_users():
	if not set(frappe.get_roles(frappe.session.user)) & USER_ADMIN_ROLES:
		frappe.throw(_("Only Management can manage users."), frappe.PermissionError)


def _validate_roles(roles: list[str]):
	unknown = [r for r in roles if r not in ASSIGNABLE_ROLES]
	if unknown:
		frappe.throw(
			_("Only these roles can be assigned here: {0}. Got: {1}").format(", ".join(ASSIGNABLE_ROLES), ", ".join(unknown)),
			frappe.ValidationError,
		)


def _serialize(doc) -> dict:
	roles = [row.role for row in doc.roles]
	return {
		"id": doc.name,
		"email": doc.email,
		"fullName": doc.full_name,
		"enabled": bool(doc.enabled),
		"appRoles": resolve_app_roles(roles),
	}


@frappe.whitelist(methods=["GET"])
def list_users(
	search: str | None = None, role: str | None = None, disabled: bool = False, limit: int = 20, offset: int = 0
):
	"""`role` is one of the four DMS role names (e.g. "DMS Warehouse") — how a picker dropdown
	gets only warehouse staff."""
	_assert_staff()
	limit, offset = clamp(limit, offset)

	filters = {"name": ["not in", SYSTEM_USERS], "user_type": "System User", "enabled": 0 if disabled else 1}
	if role:
		_validate_roles([role])
		with_role = frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent")
		filters["name"] = ["in", [n for n in with_role if n not in SYSTEM_USERS] or [""]]
	or_filters = {"full_name": ["like", f"%{search}%"], "name": ["like", f"%{search}%"]} if search else None

	names = frappe.get_all("User", filters=filters, or_filters=or_filters, pluck="name", order_by="full_name asc")
	page = names[offset : offset + limit]
	return {
		"items": [_serialize(frappe.get_doc("User", name)) for name in page],
		"total": len(names),
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["POST"])
def create_user(email: str, full_name: str, password: str, roles: list[str]):
	_assert_can_manage_users()

	email = (email or "").strip().lower()
	full_name = (full_name or "").strip()
	if not email or not full_name or not password:
		frappe.throw(_("email, full_name and password are required."), frappe.ValidationError)
	if not roles:
		frappe.throw(_("At least one role is required, otherwise this user cannot log in."), frappe.ValidationError)
	roles = list(dict.fromkeys(roles))
	_validate_roles(roles)
	if frappe.db.exists("User", email):
		frappe.throw(_("A user with email {0} already exists.").format(email), frappe.DuplicateEntryError)

	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": full_name,
			"enabled": 1,
			"user_type": "System User",
			"send_welcome_email": 0,
			"new_password": password,
			"roles": [{"role": role} for role in roles],
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_user(user: str, patch: dict):
	"""Patch keys: fullName, enabled, roles (the full desired set of DMS roles)."""
	_assert_can_manage_users()

	if user in SYSTEM_USERS:
		frappe.throw(_("{0} cannot be edited here.").format(user), frappe.PermissionError)

	doc = frappe.get_doc("User", user)
	caller_roles = set(frappe.get_roles(frappe.session.user))
	if "System Manager" in {row.role for row in doc.roles} and "System Manager" not in caller_roles:
		frappe.throw(_("Only a System Manager can edit another System Manager."), frappe.PermissionError)

	is_self = user == frappe.session.user
	if is_self and ("roles" in patch or ("enabled" in patch and not patch["enabled"])):
		frappe.throw(_("You cannot change your own roles or disable your own account."), frappe.PermissionError)

	if "fullName" in patch and patch["fullName"]:
		doc.first_name = patch["fullName"].strip()

	if "roles" in patch:
		wanted = list(dict.fromkeys(patch["roles"] or []))
		if not wanted:
			frappe.throw(
				_("At least one role is required, otherwise this user cannot log in. To deactivate a user, set enabled to false."),
				frappe.ValidationError,
			)
		_validate_roles(wanted)
		doc.set("roles", [row for row in doc.roles if row.role not in ASSIGNABLE_ROLES or row.role in wanted])
		held = {row.role for row in doc.roles}
		for role in wanted:
			if role not in held:
				doc.append("roles", {"role": role})

	disabling = "enabled" in patch and not patch["enabled"] and doc.enabled
	if "enabled" in patch:
		doc.enabled = 1 if patch["enabled"] else 0

	doc.save(ignore_permissions=True)

	if disabling:
		frappe.db.set_value("Auth Session", {"user": doc.name, "revoked_at": ["is", "not set"]}, "revoked_at", now_datetime())

	return _serialize(doc)
