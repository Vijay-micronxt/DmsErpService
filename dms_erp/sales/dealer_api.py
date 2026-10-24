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
MD-01): name, group, territory, dealer type, credit limit, salesperson, GSTIN,
address, disabled. `classification` is deliberately not settable here — it is
recomputed nightly.

`gstin`/`address` are plain Customer custom fields (sales/setup.py), free text like
`phone`/`email` — GSTIN isn't validated against the checksum format, and address
isn't a linked Address doctype record.

`portalAccountCreated`/`hasPassword`/`lastLoginAt` (see `_portal_status_for`) are
read-only staff visibility into BRD C.13 onboarding -- the Customer form itself
has no such field, since a dealer's portal User is created lazily on their first
OTP login, not at Customer-creation time, and a password is always self-service
(auth.dealer_api.set_my_password) -- there's no staff-side way to set one, so
there needed to be *some* way for staff to tell "has this dealer ever actually
logged in" apart from "we typed an email into their record."

`priceList` is ERPNext's own native Customer.default_price_list field -- unused
anywhere in this app until now. It's a manual override of BRD C.1.4's automatic
Standard/Dealer/Master classification tiering (pricing.api.get_price_for_dealer):
when set, it always wins over the dealer's classification for every quote/order
line, for a dealer with a genuine reason to be billed off a specific list
(export, a project rate, any other Price List staff have created) rather than
the volume-tier lookup. It is settable here, unlike `classification` itself --
the two are independent: the nightly recompute keeps updating
custom_dealer_classification in the background even for a tagged dealer, it's
just ignored for pricing as long as the tag is set.
"""

import frappe
from frappe import _
from frappe.utils import flt

from dms_erp.finance.ledger import party_balances
from dms_erp.phone_utils import clean_indian_mobile
from dms_erp.sales.order_channel import DEALER_TYPES
from dms_erp.warehouse.utils import default_company

SALES_WRITE_ROLES = {"DMS Sales", "DMS Management", "System Manager"}


def _assert_can_manage_dealers():
	if not set(frappe.get_roles(frappe.session.user)) & SALES_WRITE_ROLES:
		frappe.throw(_("Only Sales or Management can manage dealers."), frappe.PermissionError)


@frappe.whitelist(methods=["GET"])
def list_customer_groups(search: str | None = None):
	"""Real, assignable (leaf, non-group) Customer Groups only -- a group-type node
	(e.g. "All Customer Groups") is rejected by ERPNext itself if passed to a
	Customer, so there's no point surfacing it as a pickable option here."""
	filters = {"is_group": 0}
	if search:
		filters["name"] = ["like", f"%{search}%"]
	names = frappe.get_all("Customer Group", filters=filters, pluck="name", order_by="name asc")
	return names


def _default_parent_customer_group() -> str | None:
	"""Selling Settings' default Customer Group is usually a *leaf* (it's the group a new
	customer lands in), and ERPNext refuses a leaf as a parent -- so using it as-is made
	"create group" fail on any site whose default is a leaf (a real report from the TAS
	site). Use it only when it is itself a group; otherwise its parent; otherwise the root."""
	default = frappe.db.get_single_value("Selling Settings", "customer_group")
	if default:
		is_group, parent = frappe.db.get_value("Customer Group", default, ["is_group", "parent_customer_group"]) or (0, None)
		if is_group:
			return default
		if parent:
			return parent
	return frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ["is", "not set"]}, "name") or frappe.db.get_value(
		"Customer Group", {"is_group": 1}, "name"
	)


@frappe.whitelist(methods=["POST"])
def create_customer_group(name: str, parent: str | None = None):
	"""Creates a new leaf Customer Group so staff aren't blocked waiting on a
	System Manager to add one via the Desk tree view. `parent` defaults to the
	site's root Customer Group when omitted (same default ERPNext's own Customer
	Group tree view uses)."""
	_assert_can_manage_dealers()

	name = (name or "").strip()
	if not name:
		frappe.throw(_("name is required."), frappe.ValidationError)
	if frappe.db.exists("Customer Group", name):
		frappe.throw(_("Customer Group {0} already exists.").format(name), frappe.DuplicateEntryError)

	parent = parent or _default_parent_customer_group()
	doc = frappe.get_doc(
		{
			"doctype": "Customer Group",
			"customer_group_name": name,
			"parent_customer_group": parent,
			"is_group": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


@frappe.whitelist(methods=["GET"])
def list_price_lists(search: str | None = None):
	"""Enabled selling Price Lists, for the dealer edit form's manual price-list
	picker (default_price_list) -- includes the three classification-tier lists
	(pricing/setup.py) alongside any other selling Price List staff have created
	in Desk (export, project rates, anything else)."""
	filters = {"enabled": 1, "selling": 1}
	if search:
		filters["price_list_name"] = ["like", f"%{search}%"]
	return frappe.get_all("Price List", filters=filters, pluck="name", order_by="name asc")


def _validate_price_list(price_list: str | None):
	if price_list and not frappe.db.exists("Price List", {"name": price_list, "enabled": 1, "selling": 1}):
		frappe.throw(_("Invalid or disabled Price List: {0}").format(price_list), frappe.ValidationError)


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
	phone: str | None = None,
	price_visible: int = 1,
	email: str | None = None,
	out_of_station: int = 0,
	gstin: str | None = None,
	address: str | None = None,
	outstanding: float = 0,
	portal_status: dict | None = None,
	price_list: str | None = None,
	additional_phones: list | None = None,
) -> dict:
	portal_status = portal_status or _NO_PORTAL_ACCOUNT
	return {
		"id": name,
		"name": customer_name,
		"group": customer_group,
		"territory": territory,
		"creditLimit": credit_limit or 0,
		"disabled": bool(disabled),
		"classification": classification,
		# BRD C.1.4 — ERPNext's native Customer.default_price_list, a manual override
		# that always wins over `classification` above when set (see
		# pricing.api.get_price_for_dealer). Null means "no override, price off the
		# automatic classification tier as usual" — the common case.
		"priceList": price_list,
		"dealerType": dealer_type,
		"salesperson": salesperson,
		# BRD C.13 — the dealer portal's login identifier (see auth.dealer_api).
		"phone": phone,
		# Client requirement 8.9 -- more numbers for the same dealer; each works for OTP
		# login and WhatsApp exactly like `phone`.
		"additionalPhones": additional_phones or [],
		# BRD C.13.1 — "price (if enabled for that dealer)". Gates price in the
		# dealer portal only (sales.dealer_portal_api); staff screens are unaffected.
		"priceVisible": bool(price_visible),
		# BRD C.13 — WhatsApp + email follow-up, and (once a dealer sets their own
		# password via auth.dealer_api.set_my_password) the dealer portal's second
		# login identifier alongside phone+OTP.
		"email": email,
		# BRD C.1.4 — a floor on classification (at least Master Dealer), applied by
		# pricing.dealer_classification.recompute_dealer_classifications, not here.
		"outOfStation": bool(out_of_station),
		"gstin": gstin,
		"address": address,
		"outstanding": outstanding,
		# BRD C.13 — staff-side onboarding visibility, since nothing about a
		# dealer's portal status shows on the Customer form itself (there's no
		# "has this dealer ever logged in" field — see _portal_status_for).
		"portalAccountCreated": portal_status["portalAccountCreated"],
		"hasPassword": portal_status["hasPassword"],
		"lastLoginAt": portal_status["lastLoginAt"],
	}


_NO_PORTAL_ACCOUNT = {"portalAccountCreated": False, "hasPassword": False, "lastLoginAt": None}


def _portal_status_for(customer_names: list[str]) -> dict[str, dict]:
	"""Batch lookup of each dealer's portal-onboarding status (BRD C.13): has a
	portal User ever been created for them (auth.dealer_api._dealer_portal_user,
	created lazily on that dealer's *first successful OTP login* -- never at
	Customer-creation time, so a freshly onboarded dealer shows nothing here
	until they actually log in once), have they set a password
	(auth.dealer_api.set_my_password -- self-service only, see that function's
	own docstring for why there's no staff-side equivalent), and when they last
	obtained a session at all (OTP or password login; Auth Session.issued_at is
	also bumped on every refresh_token rotation, so this is "last active", not
	just "last fresh login").

	__Auth isn't a real doctype (no meta/permissions) -- queried the same way
	Frappe's own frappe.utils.password module does, raw SQL, never
	frappe.get_all. Returns a dense map covering every name in `customer_names`,
	defaulted to "never onboarded", so callers never need their own fallback."""
	status = {name: dict(_NO_PORTAL_ACCOUNT) for name in customer_names}
	if not customer_names:
		return status

	users = frappe.get_all("User", filters={"custom_dealer": ["in", customer_names]}, fields=["name", "custom_dealer"])
	if not users:
		return status

	user_names = [u.name for u in users]
	password_set_users = {
		row.name
		for row in frappe.db.sql(
			"select name from `__Auth` where doctype=%s and fieldname=%s and name in %s",
			("User", "password", user_names),
			as_dict=True,
		)
	}
	last_login_by_user = {
		row.user: row.last_login
		for row in frappe.db.sql(
			"""
			select user, max(issued_at) as last_login
			from `tabAuth Session`
			where user in %s
			group by user
			""",
			(user_names,),
			as_dict=True,
		)
	}

	for user in users:
		status[user.custom_dealer] = {
			"portalAccountCreated": True,
			"hasPassword": user.name in password_set_users,
			"lastLoginAt": last_login_by_user.get(user.name),
		}
	return status


def _additional_phones_for(customer_names: list[str]) -> dict[str, list[dict]]:
	if not customer_names or not frappe.db.table_exists("Dealer Phone"):
		return {}
	out: dict[str, list[dict]] = {}
	for r in frappe.get_all(
		"Dealer Phone",
		filters={"parent": ["in", customer_names], "parenttype": "Customer"},
		fields=["parent", "phone", "contact_name"],
		order_by="idx asc",
		ignore_permissions=True,
	):
		out.setdefault(r.parent, []).append({"phone": r.phone, "name": r.contact_name})
	return out


def _set_additional_phones(doc, phones) -> None:
	"""Replaces the dealer's extra numbers. Each is normalized like the main phone, and
	a number already used by another active dealer (as their main or extra number) is
	refused: OTP login and WhatsApp resolve a number to exactly one dealer, so a shared
	number would let one dealer's staff sign in as the other."""
	rows, seen = [], {doc.get("custom_phone")}
	# A dealer being created has no name yet; "!=" against NULL would match nothing.
	self_name = doc.name or "__new_dealer__"
	for entry in phones or []:
		raw = entry.get("phone") if isinstance(entry, dict) else entry
		if not raw or not str(raw).strip():
			continue
		phone = _clean_phone_or_throw(str(raw))
		if phone in seen:
			continue
		seen.add(phone)
		owner = frappe.db.get_value("Customer", {"custom_phone": phone, "disabled": 0, "name": ["!=", self_name]}, "name")
		if not owner:
			for parent in frappe.get_all(
				"Dealer Phone", filters={"phone": phone, "parenttype": "Customer", "parent": ["!=", self_name]}, pluck="parent"
			):
				if not frappe.db.get_value("Customer", parent, "disabled"):
					owner = parent
					break
		if owner:
			frappe.throw(_("{0} is already registered to another dealer ({1}).").format(phone, owner), frappe.DuplicateEntryError)
		name = (entry.get("name") or "").strip() if isinstance(entry, dict) else ""
		rows.append({"phone": phone, "contact_name": name or None})
	doc.set("custom_additional_phones", rows)


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
			"custom_phone",
			"custom_price_visible",
			"custom_email",
			"custom_out_of_station",
			"custom_gstin",
			"custom_address",
			"default_price_list",
		],
		order_by="customer_name asc",
	)
	company = default_company()
	credit_limits = _credit_limits_for([r.name for r in rows], company)
	outstanding = party_balances("Customer", [r.name for r in rows], company)
	portal_status = _portal_status_for([r.name for r in rows])
	extra_phones = _additional_phones_for([r.name for r in rows])
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
			r.custom_phone,
			r.custom_price_visible,
			r.custom_email,
			r.custom_out_of_station,
			r.custom_gstin,
			r.custom_address,
			outstanding.get(r.name, 0),
			portal_status.get(r.name),
			r.default_price_list,
			extra_phones.get(r.name),
		)
		for r in rows
	]


@frappe.whitelist(methods=["GET"])
def get_dealer(dealer: str):
	doc = frappe.get_doc("Customer", dealer)
	company = default_company()
	credit_limit = frappe.db.get_value("Customer Credit Limit", {"parent": dealer, "company": company}, "credit_limit")
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
		doc.custom_phone,
		doc.custom_price_visible,
		doc.custom_email,
		doc.custom_out_of_station,
		doc.custom_gstin,
		doc.custom_address,
		party_balances("Customer", [doc.name], company).get(doc.name, 0),
		_portal_status_for([doc.name]).get(doc.name),
		doc.default_price_list,
		[{"phone": r.phone, "name": r.contact_name} for r in doc.get("custom_additional_phones") or []],
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


def _clean_phone_or_throw(phone: str) -> str:
	"""Stored normalized (bare 10 digits — see phone_utils.clean_indian_mobile)
	so phone_utils.dealer_for_phone's lookup (used by auth.dealer_api's OTP login
	and comms.api's inbound-webhook resolution) and comms.whats91's receiverId
	both match this dealer regardless of how the number is typed here (+91,
	spaces, a leading 0). Rejected outright rather than silently dropped, since
	an un-normalizable phone here would otherwise let a dealer never be able to
	log into the portal at all without anyone noticing until they tried."""
	clean = clean_indian_mobile(phone)
	if not clean:
		frappe.throw(_("{0} is not a valid 10-digit Indian mobile number.").format(phone), frappe.ValidationError)
	return clean


def _assert_email_available(email: str, exclude: str | None = None):
	"""Guards the one place email doubles as a login credential: auth.dealer_api's
	login_with_password resolves Customer.custom_email -> dealer -> portal User, so
	two dealers sharing the same email would make that lookup pick one arbitrarily
	-- silently letting either one's password sign in as the other. No such
	uniqueness constraint exists for phone (OTP resolves it the exact same way, same
	latent risk) -- not retrofitting that here, since nothing about this feature
	depends on it; email needs it now because password login is the first thing that
	turns email into a lookup key at all. `exclude` is the dealer being edited, so
	saving a dealer's own unchanged email doesn't trip over itself."""
	existing = frappe.db.get_value("Customer", {"custom_email": email, "disabled": 0}, "name")
	if existing and existing != exclude:
		frappe.throw(_("{0} is already registered to another dealer.").format(email), frappe.ValidationError)


def _set_credit_limit(doc, credit_limit: float | None):
	"""Leaves the table untouched when nothing actually changes, and treats 0 as "no
	dealer-level limit" (removes the row) rather than a literal ₹0 limit. Both matter
	because ERPNext's Customer.validate_credit_limit_on_change re-checks every row
	against current outstanding whenever the table differs from what's saved, and
	throws if a limit is below it: the dealer edit form always sent creditLimit, so a
	dealer with no limit row (0 in the form) and real dues got a fresh ₹0 row appended
	on *any* edit -- a phone-number change failed with "New credit limit is less than
	current outstanding". 0-means-none is what sales.credit_limit.get_credit_limit
	already reads it as (falls through to Customer Group / Company), so no behaviour
	changes for the credit gate."""
	company = default_company()
	credit_limit = flt(credit_limit)
	row = next((r for r in doc.get("credit_limits") or [] if r.company == company), None)
	if (flt(row.credit_limit) if row else 0) == credit_limit:
		return
	if not credit_limit:
		doc.remove(row)
	elif row:
		row.credit_limit = credit_limit
	else:
		doc.append("credit_limits", {"company": company, "credit_limit": credit_limit})


@frappe.whitelist(methods=["POST"])
def create_dealer(
	name: str,
	group: str | None = None,
	territory: str | None = None,
	dealer_type: str | None = None,
	credit_limit: float | None = None,
	salesperson: str | None = None,
	phone: str | None = None,
	price_visible: bool | None = None,
	email: str | None = None,
	out_of_station: bool | None = None,
	gstin: str | None = None,
	address: str | None = None,
	price_list: str | None = None,
	additional_phones: list | None = None,
):
	"""BRD MD-01 — create a dealer (a native Customer). `group`/`territory` fall back to
	the site's Selling Settings defaults when omitted; a group-type Customer Group is
	rejected by ERPNext itself, so pass a real leaf group. `price_visible` left unset
	takes the field's own default (visible — BRD C.13.1). `out_of_station` only takes
	effect at the next classification recompute (BRD C.1.4), not immediately.
	`price_list`, left unset, prices this dealer off the automatic classification tier
	as usual — see pricing.api.get_price_for_dealer."""
	_assert_can_manage_dealers()

	name = (name or "").strip()
	if not name:
		frappe.throw(_("A dealer name is required."), frappe.ValidationError)
	# A renamed dealer keeps its original id, so the name can be taken as an id even when no dealer is displayed under it.
	if frappe.db.exists("Customer", name) or frappe.db.exists("Customer", {"customer_name": name}):
		frappe.throw(_("A dealer named {0} already exists.").format(name), frappe.DuplicateEntryError)
	_validate_dealer_type(dealer_type)
	_validate_price_list(price_list)

	values = {"doctype": "Customer", "customer_name": name, "customer_type": "Company"}
	if group:
		values["customer_group"] = group
	if territory:
		values["territory"] = territory
	if dealer_type:
		values["custom_dealer_type"] = dealer_type
	if salesperson:
		values["custom_salesperson"] = salesperson
	if phone:
		values["custom_phone"] = _clean_phone_or_throw(phone)
	if price_visible is not None:
		values["custom_price_visible"] = 1 if price_visible else 0
	if email:
		email = email.strip()
		_assert_email_available(email)
		values["custom_email"] = email
	if out_of_station is not None:
		values["custom_out_of_station"] = 1 if out_of_station else 0
	if gstin:
		values["custom_gstin"] = gstin.strip().upper()
	if address:
		values["custom_address"] = address.strip()
	if price_list:
		values["default_price_list"] = price_list

	doc = frappe.get_doc(values)
	if additional_phones:
		_set_additional_phones(doc, additional_phones)
	if credit_limit is not None:
		_set_credit_limit(doc, credit_limit)
	doc.insert(ignore_permissions=True)
	return get_dealer(doc.name)


@frappe.whitelist(methods=["POST", "PUT"])
def update_dealer(dealer: str, patch: dict):
	"""Patch keys: name, group, territory, dealerType, salesperson, creditLimit, disabled,
	phone, priceVisible, email, outOfStation, gstin, address, priceList. Anything else —
	including `classification`, which is recomputed nightly — is ignored.

	`priceList` (null to clear) is the manual override of the automatic classification
	tier — see pricing.api.get_price_for_dealer and dealer_api's own module docstring."""
	_assert_can_manage_dealers()

	field_map = {
		"name": "customer_name",
		"group": "customer_group",
		"territory": "territory",
		"dealerType": "custom_dealer_type",
		"salesperson": "custom_salesperson",
		"address": "custom_address",
		"priceList": "default_price_list",
	}
	if "dealerType" in patch:
		_validate_dealer_type(patch["dealerType"])
	if "priceList" in patch:
		_validate_price_list(patch["priceList"])
	if patch.get("email"):
		_assert_email_available(patch["email"].strip(), exclude=dealer)

	doc = frappe.get_doc("Customer", dealer)
	if "name" in patch:
		new_name = (patch["name"] or "").strip()
		if not new_name:
			frappe.throw(_("A dealer name is required."), frappe.ValidationError)
		taken = frappe.db.exists("Customer", {"customer_name": new_name, "name": ["!=", dealer]}) or (
			new_name != dealer and frappe.db.exists("Customer", new_name)
		)
		if taken:
			frappe.throw(_("A dealer named {0} already exists.").format(new_name), frappe.DuplicateEntryError)
		patch = {**patch, "name": new_name}
	for key, value in patch.items():
		if key == "phone":
			doc.set("custom_phone", _clean_phone_or_throw(value) if value else None)
		elif key == "gstin":
			doc.set("custom_gstin", value.strip().upper() if value else None)
		elif key == "email":
			doc.set("custom_email", value.strip() if value else None)
		elif key == "priceVisible":
			doc.set("custom_price_visible", 1 if value else 0)
		elif key == "outOfStation":
			doc.set("custom_out_of_station", 1 if value else 0)
		elif key in field_map:
			doc.set(field_map[key], value)
		elif key == "creditLimit":
			_set_credit_limit(doc, value)
		elif key == "disabled":
			doc.disabled = 1 if value else 0
		elif key == "additionalPhones":
			_set_additional_phones(doc, value)
	doc.save(ignore_permissions=True)
	return get_dealer(doc.name)
