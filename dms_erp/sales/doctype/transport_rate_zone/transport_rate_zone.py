"""ISS-2026-00633 — "Transport pricing by pincode/distance." No real pincode/zone
rate table exists yet (only Pacific has it, per the MOM); this is an empty-by-default
master Management/Sales can fill in as real rates become available. Deliberately
exact-pincode lookup only, no zone/distance-band logic -- a dealer's own delivery
pincode (Quotation.custom_delivery_pincode) is matched against this doctype's name
(autoname: field:pincode) by sales.quotation_api.suggest_transport_rate, which
returns None (never a fabricated number) when no row exists for that exact pincode.
"""

from frappe.model.document import Document


class TransportRateZone(Document):
	pass
