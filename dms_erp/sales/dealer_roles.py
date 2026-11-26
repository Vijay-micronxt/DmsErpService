"""Who at a dealer may do what in the dealer portal (client request, 24 Nov 2026).

A dealer has several people (the main phone plus Customer.custom_additional_phones), and each
number signs in as its OWN portal User (auth.dealer_api._dealer_portal_user), so a role can
differ per person. The role sits on the phone -- Customer.custom_phone_role for the main
number, Dealer Phone.role for the extras -- and is read from there on every request, never
copied onto the User: when staff change a role on the Customer the person's very next call
obeys it, with no second record to keep in step.

What a role may do is NOT in code: DMS Sales Settings holds one tick box per role per
permission, so Pacific changes it in Desk with no deploy. The defaults below only apply to a
site where that form has never been saved (a Single doctype returns None until then):
Dealer Accounts gets everything -- every dealer user that existed before roles did becomes an
Accounts user, so nothing disappears on deploy -- and Dealer Sales starts with nothing,
because a permission nobody chose should be off, not on.

A blank role is treated as Dealer Accounts for the same reason: an extra number added before
this feature existed already worked, and must keep working.
"""

import frappe
from frappe import _
from frappe.utils import cint

ACCOUNTS = "Dealer Accounts"
SALES = "Dealer Sales"
ROLES = (ACCOUNTS, SALES)
DEFAULT_ROLE = ACCOUNTS

# permission -> what the dealer sees in the refusal ("You don't have permission to <this>.")
PERMISSIONS = {
	"see_price": "see prices",
	"view_quotations": "view quotations",
	"view_orders": "view sales orders",
	"raise_order": "raise a sales order",
	"view_invoices": "view invoices",
}

_ROLE_PREFIX = {ACCOUNTS: "dealer_accounts_can_", SALES: "dealer_sales_can_"}
_DEFAULT_ON = {ACCOUNTS: True, SALES: False}

# Every key a dealer-facing document uses for money. A recursive strip on these (rather than a
# per-serializer allow-list) is deliberate: the order, quotation, invoice and enquiry shapes all
# embed one another (an order carries its invoices, an invoice its taxes and receipts), so a
# field-by-field list would leak the one nesting somebody forgot.
PRICE_KEYS = frozenset(
	{
		"priceListRate",
		"discountPercentage",
		"rate",
		"amount",
		"netTotal",
		"total",
		"grandTotal",
		"taxTotal",
		"totalTaxesAndCharges",
		"totalPayable",
		"freight",
		"taxes",
		"roundingAdjustment",
		"prices",
		"price",
		"dealerPrice",
		"receipts",
	}
)


def setting_field(role: str, permission: str) -> str:
	return _ROLE_PREFIX[role] + permission


def role_for_user(user: str | None = None) -> str | None:
	"""The role of whoever is signed in, or None when this account is not a dealer user or its
	phone is no longer one of the dealer's numbers (a removed number must stop working)."""
	user = user or frappe.session.user
	dealer, phone = frappe.db.get_value("User", user, ["custom_dealer", "custom_dealer_phone"]) or (None, None)
	if not dealer:
		return None
	main_phone, main_role = frappe.db.get_value("Customer", dealer, ["custom_phone", "custom_phone_role"]) or (None, None)
	# The account created before per-phone users existed has no phone of its own: it is the main number's.
	if not phone or phone == main_phone:
		return main_role or DEFAULT_ROLE
	# pluck returns [] for no such number, and [None] for a number whose role is blank -- two cases a
	# plain get_value cannot tell apart.
	rows = frappe.get_all(
		"Dealer Phone",
		filters={"parent": dealer, "parenttype": "Customer", "phone": phone},
		pluck="role",
		limit=1,
		ignore_permissions=True,
	)
	if not rows:
		return None
	return rows[0] or DEFAULT_ROLE


def _ticks(role: str) -> dict[str, bool]:
	"""The tick boxes for one role. Read from tabSingles itself: frappe's get_single_value answers 0
	for a box on a form that was never saved, which is indistinguishable from "unticked" -- and the
	two must differ, since the shipped default for Dealer Accounts is ON."""
	fields = {setting_field(role, permission): permission for permission in PERMISSIONS}
	stored = dict(
		frappe.db.sql(
			"select field, value from `tabSingles` where doctype = 'DMS Sales Settings' and field in %s",
			(tuple(fields),),
		)
	)
	return {
		permission: (_DEFAULT_ON[role] if stored.get(field) is None else bool(cint(stored[field])))
		for field, permission in fields.items()
	}


def role_allows(role: str, permission: str) -> bool:
	return _ticks(role)[permission]


def permissions_for_user(user: str | None = None) -> list[str]:
	role = role_for_user(user)
	if role not in ROLES:
		return []
	ticks = _ticks(role)
	return [permission for permission in PERMISSIONS if ticks[permission]]


def can(permission: str) -> bool:
	return permission in permissions_for_user()


def require(permission: str) -> None:
	"""The one gate every dealer-portal endpoint calls first. The wording names the action and
	who to ask, never the role or the setting behind it."""
	if not can(permission):
		frappe.throw(
			_("You don't have permission to {0}. Please ask the person who manages your account with Pacific.").format(
				_(PERMISSIONS[permission])
			),
			frappe.PermissionError,
		)


def strip_prices(data):
	"""`data` with every money field removed, at any depth."""
	if isinstance(data, dict):
		return {key: strip_prices(value) for key, value in data.items() if key not in PRICE_KEYS}
	if isinstance(data, list):
		return [strip_prices(item) for item in data]
	return data


def price_filtered(data):
	"""What a dealer-facing response should carry: untouched when this person may see prices."""
	return data if can("see_price") else strip_prices(data)
