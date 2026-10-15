"""ISS-2026-00633 -- "Transport Rate master: from pincode / zone -> to pincode / zone
-> rate per kg or per km (by vehicle type)."

One row is one delivery zone: a pincode range (from/to, inclusive -- a single
pincode is a range of one), an optional vehicle type, and a rate on one of three
bases. The "from" side of the ticket is always Pacific's own warehouse, so it isn't
a field; only the delivery end varies. Per Km has no live road-distance source to
multiply against (no maps/distance API is integrated), so each Per Km row carries
its own distance_km instead of the code guessing one.

Ranges for the same vehicle type may not overlap: sales.quotation_api.
suggest_transport_rate returns every row that covers a pincode, and two rows for
the same vehicle disagreeing on one pincode would leave staff with two "right"
answers. Different vehicle types over the same range are the intended case.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document

_PINCODE_RE = re.compile(r"^\d{6}$")


class TransportRateZone(Document):
	def validate(self):
		self.from_pincode = (self.from_pincode or "").strip()
		self.to_pincode = (self.to_pincode or "").strip() or self.from_pincode
		self.vehicle_type = (self.vehicle_type or "").strip() or None
		for label, value in ((_("From Pincode"), self.from_pincode), (_("To Pincode"), self.to_pincode)):
			if not _PINCODE_RE.match(value):
				frappe.throw(_("{0} must be a 6-digit pincode.").format(label))
		# Same-length digit strings compare the same as the numbers they spell, so the
		# range check (here and in the lookup's SQL) can stay on the stored strings.
		if self.from_pincode > self.to_pincode:
			frappe.throw(_("From Pincode must not be after To Pincode."))
		if (self.rate or 0) <= 0:
			frappe.throw(_("Rate must be greater than zero."))
		if self.rate_basis == "Per Km" and (self.distance_km or 0) <= 0:
			frappe.throw(_("Enter the road distance in km for a Per Km rate."))
		self._reject_overlap()

	def _reject_overlap(self):
		clash = frappe.db.sql(
			"""
			select zone_name from `tabTransport Rate Zone`
			where name != %(name)s
				and coalesce(vehicle_type, '') = %(vehicle_type)s
				and from_pincode <= %(to)s and to_pincode >= %(from)s
			limit 1
			""",
			{
				"name": self.name or "",
				"vehicle_type": self.vehicle_type or "",
				"from": self.from_pincode,
				"to": self.to_pincode,
			},
		)
		if clash:
			frappe.throw(
				_("This pincode range overlaps zone {0} for the same vehicle type.").format(clash[0][0])
			)
