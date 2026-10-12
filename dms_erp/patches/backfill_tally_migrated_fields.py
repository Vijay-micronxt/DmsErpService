"""One-time backfill for Tally-migrated Customers and Items: the migration wrote
contact/GST/address data into ERPNext's standard places (Contact, Address,
Customer.gstin) and item attributes into tally_* fields, but every dms_erp API
reads the custom_* fields instead, so the app showed them blank.

Only fills fields that are still empty -- never overwrites anything a user has
since entered -- and no-ops on sites that never ran the Tally migration.
"""

import re
from collections import defaultdict

import frappe

from dms_erp.phone_utils import clean_indian_mobile

GSTIN_RE = re.compile(r"^\d{2}[A-Z0-9]{13}$")
# "080-22862250" is a Bangalore landline, but clean_indian_mobile strips the leading
# 0 and accepts "8022862250" as a mobile -- an OTP/WhatsApp number must be a real mobile.
STD_LANDLINE_RE = re.compile(r"^\s*0\d{2,4}[-\s]")


def _mobile(raw: str | None) -> str | None:
	if STD_LANDLINE_RE.match(raw or ""):
		return None
	return clean_indian_mobile(raw)


def execute():
	if frappe.db.has_column("Customer", "tally_guid"):
		_backfill_dealers()
	if frappe.db.has_column("Item", "tally_pieces_per_box"):
		_backfill_items()


def _backfill_dealers():
	customers = {
		r.name: r
		for r in frappe.db.sql(
			"""select name, gstin, custom_phone, custom_email, custom_gstin, custom_address
			from `tabCustomer` where coalesce(tally_guid, '') != ''""",
			as_dict=True,
		)
	}
	if not customers:
		return

	contacts_by_customer = defaultdict(list)
	for r in frappe.db.sql(
		"""select link_name, parent from `tabDynamic Link`
		where link_doctype = 'Customer' and parenttype = 'Contact'""",
		as_dict=True,
	):
		contacts_by_customer[r.link_name].append(r.parent)

	phones_by_contact = defaultdict(list)
	for r in frappe.db.sql(
		"""select parent, phone from `tabContact Phone`
		order by is_primary_mobile_no desc, is_primary_phone desc, idx""",
		as_dict=True,
	):
		phones_by_contact[r.parent].append(r.phone)

	emails_by_contact = defaultdict(list)
	for r in frappe.db.sql(
		"select parent, email_id from `tabContact Email` order by is_primary desc, idx", as_dict=True
	):
		if (r.email_id or "").strip():
			emails_by_contact[r.parent].append(r.email_id.strip())

	phone_for, email_for = {}, {}
	for customer, contacts in contacts_by_customer.items():
		if customer not in customers:
			continue
		phones = [p for c in contacts for p in map(_mobile, phones_by_contact[c]) if p]
		if phones:
			phone_for[customer] = phones[0]
		emails = [e for c in contacts for e in emails_by_contact[c]]
		if emails:
			email_for[customer] = emails[0]

	# custom_phone is the dealer-portal OTP login key (phone_utils.dealer_for_phone),
	# so a number shared by two dealers would log one into the other's account.
	customers_per_phone = defaultdict(list)
	for customer, phone in phone_for.items():
		customers_per_phone[phone].append(customer)
	for phone, owners in customers_per_phone.items():
		if len(owners) > 1:
			for customer in owners:
				phone_for.pop(customer)

	address_for = {}
	for r in frappe.db.sql(
		"""select dl.link_name, a.address_line1, a.address_line2, a.city, a.state, a.pincode
		from `tabDynamic Link` dl join `tabAddress` a on a.name = dl.parent
		where dl.link_doctype = 'Customer' and dl.parenttype = 'Address'
		order by a.is_primary_address desc, a.creation""",
		as_dict=True,
	):
		if r.link_name in address_for or r.link_name not in customers:
			continue
		parts = []
		for part in (r.address_line1, r.address_line2, r.city, r.state, r.pincode):
			part = (part or "").strip()
			if part and not any(part in existing for existing in parts):
				parts.append(part)
		if parts:
			address_for[r.link_name] = ", ".join(parts)

	for name, c in customers.items():
		values = {}
		if not c.custom_phone and name in phone_for:
			values["custom_phone"] = phone_for[name]
		if not c.custom_email and name in email_for:
			values["custom_email"] = email_for[name]
		gstin = (c.gstin or "").strip().upper()
		if not c.custom_gstin and GSTIN_RE.match(gstin):
			values["custom_gstin"] = gstin
		if not c.custom_address and name in address_for:
			values["custom_address"] = address_for[name]
		if values:
			frappe.db.set_value("Customer", name, values, update_modified=False)


def _backfill_items():
	known_hsn = set(frappe.get_all("GST HSN Code", pluck="name")) if frappe.db.exists("DocType", "GST HSN Code") else set()
	items = frappe.db.sql(
		"""select name, tally_pieces_per_box, tally_width_mm, tally_height_mm, tally_weight_per_piece,
			tally_hsn_code, custom_pieces_per_box, custom_size, custom_weight_per_box_kg, gst_hsn_code
		from `tabItem` where coalesce(tally_guid, '') != ''""",
		as_dict=True,
	)
	for i in items:
		pieces = i.tally_pieces_per_box or 0
		values = {}
		if not i.custom_pieces_per_box and pieces > 0:
			values["custom_pieces_per_box"] = pieces
		if not i.custom_size and (i.tally_width_mm or 0) > 0 and (i.tally_height_mm or 0) > 0:
			values["custom_size"] = f"{round(i.tally_width_mm)}x{round(i.tally_height_mm)}mm"
		if not i.custom_weight_per_box_kg and pieces > 0 and (i.tally_weight_per_piece or 0) > 0:
			values["custom_weight_per_box_kg"] = round(pieces * i.tally_weight_per_piece, 3)
		if not i.gst_hsn_code and i.tally_hsn_code in known_hsn:
			values["gst_hsn_code"] = i.tally_hsn_code
		if values:
			frappe.db.set_value("Item", i.name, values, update_modified=False)
