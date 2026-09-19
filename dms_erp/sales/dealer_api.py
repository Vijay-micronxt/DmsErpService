"""Dealer directory — every other module (Inquiry.dealer, Quotation.party_name,
Sales Order.customer, Dealer Catalog, WhatsApp Message) already treats "dealer" as
a bare native `Customer` (see dashboard/api.py's credit-exposure alert). This
module adds no doctype: it's the read endpoint that surface was always missing —
list/get over `Customer`.

Credit limit lives on the Customer Credit Limit child table (keyed by company), not
a flat Customer.credit_limit column — see dashboard/api.py::_credit_exposure_alerts
for why. Resolved here against this app's own default_company(), same as everywhere
else that needs "which company does this app's data belong to".

`classification` (Standard Dealer/Dealer/Master Dealer, BRD C.1.4) and `dealerType`
(Retail/Bulk/Project, BRD C.4.3) are two of the Customer custom fields sales/setup.py
adds — see pricing.dealer_classification and sales.order_channel for what reads
and writes them. Both are read-only here; classification is recomputed nightly and
dealerType has no write endpoint of its own yet.

`salesperson` (BRD C.12.3 — ownership of the dealer relationship) is written by
`set_dealer_salesperson`. Targets/performance-vs-target reporting is a deliberate
follow-up, not built here — see sales/setup.py's custom_salesperson field description
for why.

`create_dealer` / `update_dealer` cover the rest of the master-data write side (BRD
MD-01): name, group, territory, dealer type, credit limit, salesperson, disabled.
`classification` is deliberately not settable here — it is recomputed nightly.
"""

import frappe
from frappe import _

from dms_erp.sales.order_channel import DEALER_TYPES
from dms_erp.warehouse.utils import default_company

SALES_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}


def _assert_can_manage_dealers():
	if not set(frappe.get_roles(frappe.session.user)) & SALES_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage dealers."), frappe.PermissionError)


def _serialize(
	name: str,
	customer_name: str,
	customer_group: str | None,
	territory: str | None,
	credit_limit: float | None,
	disabled: int,
	classification: str | None = None,
	dealer_type: str | None = None,
	salesperson: str | None = None,
) -> dict:
	return {
		"id": name,
		"name": customer_name,
		"group": customer_group,
		"territory": territory,
		"creditLimit": credit_limit or 0,
		"disabled": bool(disabled),
		"classification": classification,
		"dealerType": dealer_type,
		"salesperson": salesperson,
	}


def _credit_limits_for(customer_names: list[str], company: str) -> dict[str, float]:
	if not customer_names:
		return {}
	rows = frappe.get_all(
		"Customer Credit Limit",
		filters={"parent": ["in", customer_names], "company": company},
		fields=["parent", "credit_limit"],
	)
	return {r.parent: r.credit_limit for r in rows}


@frappe.whitelist(methods=["GET"])
def list_dealers(search: str | None = None, disabled: bool = False):
	filters = {"disabled": ["=", 1 if disabled else 0]}
	if search:
		filters["customer_name"] = ["like", f"%{search}%"]
	rows = frappe.get_all(
		"Customer",
		filters=filters,
		fields=[
			"name",
			"customer_name",
			"customer_group",
			"territory",
			"disabled",
			"custom_dealer_classification",
			"custom_dealer_type",
			"custom_salesperson",
		],
		order_by="customer_name asc",
	)
	credit_limits = _credit_limits_for([r.name for r in rows], default_company())
	return [
		_serialize(
			r.name,
			r.customer_name,
			r.customer_group,
			r.territory,
			credit_limits.get(r.name),
			r.disabled,
			r.custom_dealer_classification,
			r.custom_dealer_type,
			r.custom_salesperson,
		)
		for r in rows
	]


@frappe.whitelist(methods=["GET"])
def get_dealer(dealer: str):
	doc = frappe.get_doc("Customer", dealer)
	credit_limit = frappe.db.get_value("Customer Credit Limit", {"parent": dealer, "company": default_company()}, "credit_limit")
	return _serialize(
		doc.name,
		doc.customer_name,
		doc.customer_group,
		doc.territory,
		credit_limit,
		doc.disabled,
		doc.custom_dealer_classification,
		doc.custom_dealer_type,
		doc.custom_salesperson,
	)


@frappe.whitelist(methods=["POST", "PUT"])
def set_dealer_salesperson(dealer: str, salesperson: str | None):
	"""BRD C.12.3 — assign (or clear, with salesperson=None) ownership of a dealer
	relationship."""
	_assert_can_manage_dealers()

	frappe.db.set_value("Customer", dealer, "custom_salesperson", salesperson)
	return get_dealer(dealer)


def _validate_dealer_type(dealer_type: str | None):
	if dealer_type and dealer_type not in DEALER_TYPES:
		frappe.throw(_("Invalid dealer type: {0}").format(dealer_type), frappe.ValidationError)


def _set_credit_limit(doc, credit_limit: float | None):
	company = default_company()
	row = next((r for r in doc.get("credit_limits") or [] if r.company == company), None)
	if row:
		row.credit_limit = credit_limit or 0
	else:
		doc.append("credit_limits", {"company": company, "credit_limit": credit_limit or 0})


@frappe.whitelist(methods=["POST"])
def create_dealer(
	name: str,
	group: str | None = None,
	territory: str | None = None,
	dealer_type: str | None = None,
	credit_limit: float | None = None,
	salesperson: str | None = None,
):
	"""BRD MD-01 — create a dealer (a native Customer). `group`/`territory` fall back to
	the site's Selling Settings defaults when omitted; a group-type Customer Group is
	rejected by ERPNext itself, so pass a real leaf group."""
	_assert_can_manage_dealers()

	name = (name or "").strip()
	if not name:
		frappe.throw(_("A dealer name is required."), frappe.ValidationError)
	if frappe.db.exists("Customer", {"customer_name": name}):
		frappe.throw(_("A dealer named {0} already exists.").format(name), frappe.DuplicateEntryError)
	_validate_dealer_type(dealer_type)

	values = {"doctype": "Customer", "customer_name": name, "customer_type": "Company"}
	if group:
		values["customer_group"] = group
	if territory:
		values["territory"] = territory
	if dealer_type:
		values["custom_dealer_type"] = dealer_type
	if salesperson:
		values["custom_salesperson"] = salesperson

	doc = frappe.get_doc(values)
	if credit_limit is not None:
		_set_credit_limit(doc, credit_limit)
	doc.insert(ignore_permissions=True)
	return get_dealer(doc.name)


@frappe.whitelist(methods=["POST", "PUT"])
def update_dealer(dealer: str, patch: dict):
	"""Patch keys: name, group, territory, dealerType, salesperson, creditLimit, disabled.
	Anything else — including `classification`, which is recomputed nightly — is ignored."""
	_assert_can_manage_dealers()

	field_map = {
		"name": "customer_name",
		"group": "customer_group",
		"territory": "territory",
		"dealerType": "custom_dealer_type",
		"salesperson": "custom_salesperson",
	}
	if "dealerType" in patch:
		_validate_dealer_type(patch["dealerType"])

	doc = frappe.get_doc("Customer", dealer)
	for key, value in patch.items():
		if key in field_map:
			doc.set(field_map[key], value)
		elif key == "creditLimit":
			_set_credit_limit(doc, value)
		elif key == "disabled":
			doc.disabled = 1 if value else 0
	doc.save(ignore_permissions=True)
	return get_dealer(doc.name)
