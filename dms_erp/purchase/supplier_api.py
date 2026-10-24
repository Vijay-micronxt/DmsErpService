"""Supplier directory — every other module (Purchase Order.supplier, Item Price
Proposal.supplier, Bay Allocation.supplier, Inward Truck.supplier) already treats
"supplier" as a bare native `Supplier`, with no custom fields added anywhere. This
module adds no doctype: it's the read endpoint that surface was always missing —
list/get over `Supplier`, the same gap `sales.dealer_api` closed for `Customer`.

Unlike Customer, Supplier has no per-company credit-limit child table to net out —
there's genuinely nothing else to resolve here beyond the native fields.

`custom_latitude`/`custom_longitude`/`custom_address`/`custom_contact_person` (BRD
C.1.6) are the exception: plain Custom Fields (purchase/setup.py) for GPS capture
(a prerequisite for route optimisation, BRD C.5), factory address (pickup stop
identification) and the material-ready contact (vendor enquiry/follow-up).
`set_supplier_location` is the only narrow write endpoint; `create_supplier` /
`update_supplier` cover the rest of the master-data write side (BRD MD-02): name,
group, country, GPS, address, contact, disabled.
"""

import re

import frappe
from frappe import _

SUPPLIER_WRITE_ROLES = {"DMS Purchase", "DMS Management", "System Manager"}


_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def _supplier_banks(suppliers: list[str]) -> dict[str, dict]:
	"""Client finding 7.3 -- each supplier's own (non-company) default Bank Account, in
	ERPNext's native Bank Account doctype so Payment Entries see the same record."""
	if not suppliers:
		return {}
	out = {}
	for r in frappe.get_all(
		"Bank Account",
		filters={"party_type": "Supplier", "party": ["in", suppliers], "is_company_account": 0, "disabled": 0},
		fields=["name", "party", "bank", "bank_account_no", "branch_code", "account_name", "is_default"],
		order_by="is_default desc, creation asc",
	):
		out.setdefault(
			r.party,
			{"bankName": r.bank, "accountNumber": r.bank_account_no, "ifsc": r.branch_code, "accountName": r.account_name},
		)
	return out


def _set_supplier_bank(supplier: str, bank: dict | None) -> None:
	"""Upserts the supplier's default Bank Account; an empty/None `bank` removes it.
	Only the record this app manages (the supplier's default, non-company account) is
	touched -- any other bank accounts set up in Desk are left alone."""
	existing = frappe.db.get_value(
		"Bank Account",
		{"party_type": "Supplier", "party": supplier, "is_company_account": 0, "is_default": 1},
		"name",
	)
	bank = bank or {}
	bank_name = (bank.get("bankName") or "").strip()
	account_no = (bank.get("accountNumber") or "").strip()
	ifsc = (bank.get("ifsc") or "").strip().upper()
	if not (bank_name or account_no or ifsc):
		if existing:
			frappe.delete_doc("Bank Account", existing, ignore_permissions=True)
		return
	if not bank_name or not account_no:
		frappe.throw(_("Enter both the bank name and the account number."), frappe.ValidationError)
	if ifsc and not _IFSC_RE.match(ifsc):
		frappe.throw(_("{0} isn't a valid IFSC code (11 characters, like HDFC0001234).").format(ifsc), frappe.ValidationError)
	if not frappe.db.exists("Bank", bank_name):
		frappe.get_doc({"doctype": "Bank", "bank_name": bank_name}).insert(ignore_permissions=True)

	values = {
		"bank": bank_name,
		"bank_account_no": account_no,
		"branch_code": ifsc or None,
		"party_type": "Supplier",
		"party": supplier,
		"is_company_account": 0,
		"is_default": 1,
	}
	if existing:
		doc = frappe.get_doc("Bank Account", existing)
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		supplier_name = frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier
		frappe.get_doc({"doctype": "Bank Account", "account_name": (bank.get("accountName") or supplier_name).strip(), **values}).insert(
			ignore_permissions=True
		)


def _gstin_field() -> str | None:
	"""india_compliance adds Supplier.gstin; a site without that app has no GSTIN field."""
	return "gstin" if frappe.get_meta("Supplier").has_field("gstin") else None


def _assert_can_manage_suppliers():
	if not set(frappe.get_roles(frappe.session.user)) & SUPPLIER_WRITE_ROLES:
		frappe.throw(_("Only Purchase or Management can manage suppliers."), frappe.PermissionError)


def _serialize(
	name: str,
	supplier_name: str,
	supplier_group: str | None,
	country: str | None,
	disabled: int,
	latitude=None,
	longitude=None,
	insurance_holder: str | None = None,
	address: str | None = None,
	contact_person: str | None = None,
	moq: int = 0,
	production_moq: int = 0,
	gstin: str | None = None,
	phone: str | None = None,
	email: str | None = None,
	bank: dict | None = None,
) -> dict:
	# Unset Float fields read back as 0.0, which the staff app showed as "GPS 0, 0" for
	# every supplier without a location -- 0,0 (open sea off West Africa) is never a real
	# factory, so treat it as "not captured".
	if not latitude and not longitude:
		latitude = longitude = None
	return {
		"id": name,
		"name": supplier_name,
		"group": supplier_group,
		"country": country,
		"disabled": bool(disabled),
		"latitude": latitude,
		"longitude": longitude,
		# BRD C.1.6 — which insurer a damage claim against this supplier routes to.
		"insuranceHolder": insurance_holder,
		# BRD C.1.6 — factory location, for pickup stop identification.
		"address": address,
		# BRD C.1.6 — who to reach for vendor enquiry / material-ready follow-up.
		"contactPerson": contact_person,
		# BRD C.4.2 -- this vendor's own floor, between the Item's and the company default.
		"moq": moq or 0,
		"productionMoq": production_moq or 0,
		# Client finding 7.3. GSTIN lives on india_compliance's own Supplier.gstin field
		# (validated by that app), so it's None on a site without india_compliance.
		"gstin": gstin,
		"phone": phone,
		"email": email,
		# {bankName, accountNumber, ifsc, accountName} or None -- see _supplier_banks.
		"bank": bank,
	}


@frappe.whitelist(methods=["GET"])
def list_suppliers(search: str | None = None, disabled: bool = False):
	filters = {"disabled": ["=", 1 if disabled else 0]}
	if search:
		filters["supplier_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all(
		"Supplier",
		filters=filters,
		fields=[
			"name",
			"supplier_name",
			"supplier_group",
			"country",
			"disabled",
			"custom_latitude",
			"custom_longitude",
			"custom_insurance_holder",
			"custom_address",
			"custom_contact_person",
			"custom_moq",
			"custom_production_moq",
			"custom_phone",
			"custom_email",
			*([_gstin_field()] if _gstin_field() else []),
		],
		order_by="supplier_name asc",
	)
	banks = _supplier_banks([r.name for r in rows])
	return [
		_serialize(
			r.name,
			r.supplier_name,
			r.supplier_group,
			r.country,
			r.disabled,
			r.custom_latitude,
			r.custom_longitude,
			r.custom_insurance_holder,
			r.custom_address,
			r.custom_contact_person,
			r.custom_moq,
			r.custom_production_moq,
			r.get("gstin"),
			r.custom_phone,
			r.custom_email,
			banks.get(r.name),
		)
		for r in rows
	]


@frappe.whitelist(methods=["GET"])
def get_supplier(supplier: str):
	doc = frappe.get_doc("Supplier", supplier)
	return _serialize(
		doc.name,
		doc.supplier_name,
		doc.supplier_group,
		doc.country,
		doc.disabled,
		doc.custom_latitude,
		doc.custom_longitude,
		doc.custom_insurance_holder,
		doc.custom_address,
		doc.custom_contact_person,
		doc.custom_moq,
		doc.custom_production_moq,
		doc.get("gstin"),
		doc.get("custom_phone"),
		doc.get("custom_email"),
		_supplier_banks([doc.name]).get(doc.name),
	)


@frappe.whitelist(methods=["POST", "PUT"])
def set_supplier_location(supplier: str, latitude: float, longitude: float):
	_assert_can_manage_suppliers()

	doc = frappe.get_doc("Supplier", supplier)
	doc.custom_latitude = latitude
	doc.custom_longitude = longitude
	doc.save(ignore_permissions=True)
	return get_supplier(doc.name)


@frappe.whitelist(methods=["POST"])
def create_supplier(
	name: str,
	group: str | None = None,
	country: str | None = None,
	latitude: float | None = None,
	longitude: float | None = None,
	insurance_holder: str | None = None,
	address: str | None = None,
	contact_person: str | None = None,
	moq: int = 0,
	production_moq: int = 0,
	gstin: str | None = None,
	phone: str | None = None,
	email: str | None = None,
	bank: dict | None = None,
):
	"""BRD MD-02 — create a supplier (a native Supplier). `group` falls back to the site's
	Buying Settings default when omitted."""
	_assert_can_manage_suppliers()

	name = (name or "").strip()
	if not name:
		frappe.throw(_("A supplier name is required."), frappe.ValidationError)
	# A renamed supplier keeps its original id, so the name can be taken as an id even when no supplier is displayed under it.
	if frappe.db.exists("Supplier", name) or frappe.db.exists("Supplier", {"supplier_name": name}):
		frappe.throw(_("A supplier named {0} already exists.").format(name), frappe.DuplicateEntryError)

	values = {"doctype": "Supplier", "supplier_name": name}
	if group:
		values["supplier_group"] = group
	if country:
		values["country"] = country
	if latitude is not None:
		values["custom_latitude"] = latitude
	if longitude is not None:
		values["custom_longitude"] = longitude
	if insurance_holder:
		values["custom_insurance_holder"] = insurance_holder.strip()
	if address:
		values["custom_address"] = address.strip()
	if contact_person:
		values["custom_contact_person"] = contact_person.strip()
	if moq:
		values["custom_moq"] = moq
	if production_moq:
		values["custom_production_moq"] = production_moq
	if gstin and _gstin_field():
		values["gstin"] = gstin.strip().upper()
	if phone:
		values["custom_phone"] = phone.strip()
	if email:
		values["custom_email"] = email.strip()

	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	if bank:
		_set_supplier_bank(doc.name, frappe.parse_json(bank) if isinstance(bank, str) else bank)
	return get_supplier(doc.name)


@frappe.whitelist(methods=["POST", "PUT"])
def update_supplier(supplier: str, patch: dict):
	"""Patch keys: name, group, country, latitude, longitude, disabled, insuranceHolder,
	address, contactPerson, moq, productionMoq, gstin, phone, email, bank. An empty string
	clears gstin / phone / email (an omitted key leaves it as is); `bank` is
	{bankName, accountNumber, ifsc} and null/empty removes the supplier's bank account."""
	_assert_can_manage_suppliers()

	field_map = {
		"name": "supplier_name",
		"group": "supplier_group",
		"country": "country",
		"latitude": "custom_latitude",
		"longitude": "custom_longitude",
		"insuranceHolder": "custom_insurance_holder",
		"address": "custom_address",
		"contactPerson": "custom_contact_person",
		"moq": "custom_moq",
		"productionMoq": "custom_production_moq",
	}

	doc = frappe.get_doc("Supplier", supplier)
	if "name" in patch:
		new_name = (patch["name"] or "").strip()
		if not new_name:
			frappe.throw(_("A supplier name is required."), frappe.ValidationError)
		taken = frappe.db.exists("Supplier", {"supplier_name": new_name, "name": ["!=", supplier]}) or (
			new_name != supplier and frappe.db.exists("Supplier", new_name)
		)
		if taken:
			frappe.throw(_("A supplier named {0} already exists.").format(new_name), frappe.DuplicateEntryError)
		patch = {**patch, "name": new_name}
	for key, value in patch.items():
		if key in field_map:
			doc.set(field_map[key], value)
		elif key == "disabled":
			doc.disabled = 1 if value else 0
		elif key == "gstin" and _gstin_field():
			doc.set("gstin", value.strip().upper() if value else None)
		elif key == "phone":
			doc.set("custom_phone", value.strip() if value else None)
		elif key == "email":
			doc.set("custom_email", value.strip() if value else None)
	doc.save(ignore_permissions=True)
	if "bank" in patch:
		_set_supplier_bank(doc.name, patch["bank"])
	return get_supplier(doc.name)
