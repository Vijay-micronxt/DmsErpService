"""BRD C.3.3 — structured bill-to/ship-to addresses for a dealer (Customer).

Before this, `Customer.custom_address` (`sales/setup.py`) was the only address on
a dealer at all — a single free-text field, no city/state/pincode decomposition,
no way to tell a billing address from a shipping one, no way to pick a different
site-delivery address per order. `custom_address` (and `custom_gstin`, the
dealer's own GST registration) stay exactly as they are — this doesn't supersede
or deprecate either; `custom_address` is a free-text business-address field
(WhatsApp/contact-card use), unrelated to these structured per-transaction
Address records (see `dealer_api.py`'s own docstring on `Customer.
default_price_list` for the precedent: two mechanisms can coexist, the old one
just isn't used for the new purpose).

Backed by ERPNext's native `Address` doctype (Dynamic-Linked to Customer) rather
than a new custom field, so BRD C.3.3's own downstream needs — the native
`customer_address`/`shipping_address_name` fields on Sales Order/Quotation
(order_api.py, quotation_api.py), and whatever india_compliance's own
already-configured e-Way Bill module reads off an Address — all work without a
second, dms_erp-specific address shape to keep in sync. Scoped to only
`address_type` "Billing"/"Shipping" (this BRD clause's own scope), even though
the native field allows more.
"""

import frappe
from frappe import _

SALES_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
ADDRESS_TYPES = {"Billing", "Shipping"}


def _assert_can_manage_dealer_addresses():
	if not set(frappe.get_roles(frappe.session.user)) & SALES_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage dealer addresses."), frappe.PermissionError)


def _validate_address_type(address_type: str):
	if address_type not in ADDRESS_TYPES:
		frappe.throw(_("Invalid address type: {0}").format(address_type), frappe.ValidationError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"addressType": doc.address_type,
		"addressLine1": doc.address_line1,
		"addressLine2": doc.address_line2,
		"city": doc.city,
		"state": doc.state,
		"pincode": doc.pincode,
		"country": doc.country,
		"isPrimary": bool(doc.is_primary_address),
		"isShipping": bool(doc.is_shipping_address),
	}


@frappe.whitelist(methods=["GET"])
def list_dealer_addresses(dealer: str) -> list[dict]:
	names = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": dealer, "parenttype": "Address"},
		pluck="parent",
	)
	if not names:
		return []
	docs = frappe.get_all(
		"Address",
		filters={"name": ["in", names]},
		fields=[
			"name",
			"address_type",
			"address_line1",
			"address_line2",
			"city",
			"state",
			"pincode",
			"country",
			"is_primary_address",
			"is_shipping_address",
		],
		order_by="creation desc",
	)
	return [
		{
			"id": d.name,
			"addressType": d.address_type,
			"addressLine1": d.address_line1,
			"addressLine2": d.address_line2,
			"city": d.city,
			"state": d.state,
			"pincode": d.pincode,
			"country": d.country,
			"isPrimary": bool(d.is_primary_address),
			"isShipping": bool(d.is_shipping_address),
		}
		for d in docs
	]


@frappe.whitelist(methods=["POST"])
def create_dealer_address(
	dealer: str,
	address_type: str,
	address_line1: str,
	city: str,
	state: str,
	pincode: str,
	address_line2: str | None = None,
	country: str = "India",
	is_primary: bool = False,
):
	_assert_can_manage_dealer_addresses()
	if not frappe.db.exists("Customer", dealer):
		frappe.throw(_("No such dealer: {0}").format(dealer), frappe.DoesNotExistError)
	_validate_address_type(address_type)
	for label, value in (("address_line1", address_line1), ("city", city), ("state", state), ("pincode", pincode)):
		if not (value or "").strip():
			frappe.throw(_("{0} is required.").format(label), frappe.ValidationError)

	values = {
		"doctype": "Address",
		"address_type": address_type,
		"address_line1": address_line1.strip(),
		"address_line2": (address_line2 or "").strip() or None,
		"city": city.strip(),
		"state": state.strip(),
		"pincode": pincode.strip(),
		"country": country,
		"links": [{"link_doctype": "Customer", "link_name": dealer}],
	}
	# "Primary" means different things per type -- a Billing address becomes the
	# dealer's default billing address, a Shipping one their default shipping
	# address; the two are independent flags on the same native doctype.
	if is_primary:
		values["is_primary_address" if address_type == "Billing" else "is_shipping_address"] = 1

	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_dealer_address(address: str, **fields):
	_assert_can_manage_dealer_addresses()
	doc = frappe.get_doc("Address", address)
	if "address_type" in fields:
		_validate_address_type(fields["address_type"])
		doc.address_type = fields["address_type"]
	for key, column in (
		("address_line1", "address_line1"),
		("address_line2", "address_line2"),
		("city", "city"),
		("state", "state"),
		("pincode", "pincode"),
		("country", "country"),
	):
		if key in fields:
			doc.set(column, fields[key])
	if "is_primary" in fields:
		flag = "is_primary_address" if doc.address_type == "Billing" else "is_shipping_address"
		doc.set(flag, 1 if fields["is_primary"] else 0)
	doc.save(ignore_permissions=True)
	return _serialize(doc)
