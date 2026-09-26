"""Shared phone-number normalization — a top-level module (not under auth/ or
comms/) so both can import it without creating a cycle: auth.dealer_api already
imports from comms.whats91 and comms.api, so comms must not import back from auth.

Every 10-digit Indian mobile number is normalized to the same clean-digits form
regardless of how it was typed (+91, spaces, dashes, a leading 0 or 91) --
Customer.custom_phone is stored this way (see sales.dealer_api.create_dealer/
update_dealer) specifically so dealer_for_phone's lookup and comms.whats91's
receiverId can both normalize whatever the caller sent and compare/send
like-for-like, instead of an exact string match that breaks the moment a
dealer's number is typed with different formatting than it was originally
stored with.
"""

import re

import frappe


def clean_indian_mobile(phone: str | None) -> str | None:
	"""Returns the bare 10-digit number, or None if `phone` isn't a plausible
	Indian mobile number (wrong length, or doesn't start with 6-9)."""
	digits = re.sub(r"\D", "", phone or "")

	if digits.startswith("91") and len(digits) == 12:
		digits = digits[2:]
	elif digits.startswith("0") and len(digits) == 11:
		digits = digits[1:]

	if len(digits) == 10 and digits[0] in "6789":
		return digits

	return None


def dealer_for_phone(phone: str | None) -> str | None:
	"""Customer.custom_phone is stored normalized (see sales.dealer_api.create_dealer/
	update_dealer) -- normalizing the caller's input the same way here is what makes
	"+91 96202 04657", "9620204657" and "919620204657" all resolve to the same dealer,
	instead of an exact string match that only works if it was typed exactly as stored.
	Used by auth.dealer_api (OTP login) and comms.api (resolving an inbound WhatsApp
	webhook's sender to a dealer) -- both need this, which is the reason this module
	exists at the top level rather than under either of theirs (see module docstring)."""
	clean_phone = clean_indian_mobile(phone)
	if not clean_phone:
		return None
	return frappe.db.get_value("Customer", {"custom_phone": clean_phone, "disabled": 0}, "name")
