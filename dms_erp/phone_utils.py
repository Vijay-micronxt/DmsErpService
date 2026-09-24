"""Shared phone-number normalization — a top-level module (not under auth/ or
comms/) so both can import it without creating a cycle: auth.dealer_api already
imports from comms.whats91, so comms must not import back from auth.

Every 10-digit Indian mobile number is normalized to the same clean-digits form
regardless of how it was typed (+91, spaces, dashes, a leading 0 or 91) --
Customer.custom_phone is stored this way (see sales.dealer_api.create_dealer/
update_dealer) specifically so auth.dealer_api._dealer_for_phone's lookup and
comms.whats91's receiverId can both normalize whatever the caller sent and
compare/send like-for-like, instead of an exact string match that breaks the
moment a dealer's number is typed with different formatting than it was
originally stored with.
"""

import re


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
