"""Client requirement 3.1/3.2 -- desk-level permission grants for the four DMS
staff roles, mirroring the role sets each API module's own `_assert_can_manage_*`
gate already enforces server-side. Without these, Frappe's desk never shows the
quick-create "+" on a Link field (e.g. Item/Customer/Supplier/Warehouse pickers)
for a role that's otherwise fully authorized to create that record via the API --
the API-level gate and desk-level permission are two separate systems, and only
the API-level one existed until now.

Purchase Receipt (3.1) is the other half: it's a native ERPNext doctype no DMS
role has ever had rights on, so DMS Warehouse -- who needs it as the natural next
step after an Inward Truck arrives -- couldn't see or create one at all.

Idempotent and safe to re-run on every `bench migrate`, same as every other
setup_* module -- add_permission() itself no-ops a grant that's already set.
"""

from frappe.permissions import add_permission

# (doctype, role) -> ptypes to grant. Mirrors CATALOG_WRITE_ROLES (catalog/api.py),
# SALES_WRITE_ROLES (sales/dealer_api.py), SUPPLIER_WRITE_ROLES (purchase/supplier_api.py)
# and BAY_WRITE_ROLES (warehouse/bay_api.py) -- the same roles already authorized to
# create these records through the API, now also given the desk-level grant that
# makes a Link field's quick-create "+" appear.
GRANTS: dict[str, list[str]] = {
	"Item": ["DMS Purchase", "DMS Management"],
	"Customer": ["DMS Sales", "DMS Management"],
	"Supplier": ["DMS Purchase", "DMS Management"],
	"Warehouse": ["DMS Warehouse", "DMS Management"],
	# Batch has no dedicated API role-gate of its own (ensure_batch is called inline
	# from whichever flow needs one) -- granted to every role that can reach a batch
	# number field at all: inward placement, purchase receipt, warehouse scanning.
	"Batch": ["DMS Warehouse", "DMS Purchase", "DMS Management"],
	# BRD 3.1 -- DMS Warehouse needs this to view/create a Purchase Receipt as the
	# natural next step after an Inward Truck arrives; DMS Purchase already covers
	# the buying side of the same document.
	"Purchase Receipt": ["DMS Warehouse", "DMS Purchase", "DMS Management"],
}


def setup_permissions():
	for doctype, roles in GRANTS.items():
		for role in roles:
			add_permission(doctype, role, ptype="read")
			add_permission(doctype, role, ptype="create")
