"""BRD C.13 — the dealer-portal identity link. A dealer-portal User account (role
DMS Dealer, created on first successful OTP login — see dealer_api.verify_otp) is
tied back to the Customer it authenticates as via this one field. Every dealer-
portal endpoint (sales/dealer_portal_api.py) resolves its dealer identity from this
field on frappe.session.user, never from a caller-supplied dealer id -- that's the
whole security boundary a dealer session operates inside.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"User": [
		{
			"fieldname": "custom_dealer",
			"fieldtype": "Link",
			"label": "Dealer",
			"options": "Customer",
			"description": "BRD C.13 — set only on dealer-portal accounts (role DMS Dealer). Never set on a staff account.",
		},
	],
}


def setup_auth():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
