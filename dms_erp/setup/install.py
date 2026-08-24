import frappe

from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing.setup import setup_pricing
from dms_erp.warehouse.setup import setup_warehouse

# Frappe Roles that back the staff app's four roles (Sales / Warehouse / Purchase /
# Management). Prefixed with "Pacific" to avoid colliding with ERPNext's own stock
# roles ("Sales User", "Purchase User", etc). desk_access=0 because these users only
# ever talk to us through the JWT API — they have no business logging into /app.
APP_ROLES = [
	"Pacific Sales",
	"Pacific Warehouse",
	"Pacific Purchase",
	"Pacific Management",
]


def after_install():
	create_app_roles()
	setup_catalog()
	setup_pricing()
	setup_warehouse()


def after_migrate():
	create_app_roles()
	setup_catalog()
	setup_pricing()
	setup_warehouse()


def create_app_roles():
	for role in APP_ROLES:
		if frappe.db.exists("Role", role):
			continue
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role,
				"desk_access": 0,
			}
		).insert(ignore_permissions=True)
