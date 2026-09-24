"""Dealer classification (BRD C.1.4) — `custom_dealer_classification` (Standard
Dealer / Dealer / Master Dealer) is a sales-volume price *tier* on Customer,
recomputed on a schedule from confirmed Sales Order value. It drives which Price
List a quotation reads its rate from (pricing.api.get_price_for_dealer) — the
classification value is always also a valid Price List name (pricing/setup.py),
so no separate mapping table is needed.

BRD Part G leaves the classification-band *measurement window* an open point
("confirm the measurement period / rolling window") while confirming the bands
themselves — DMS Sales Settings.dealer_classification_window_days is the
configurable placeholder for that still-open decision; the bands below are the
BRD's own "confirmed initial bands", not configurable here.
"""

import frappe
from frappe.utils import add_days, today

DEALER_CLASSIFICATION_STANDARD = "Standard Dealer"
DEALER_CLASSIFICATION_DEALER = "Dealer"
DEALER_CLASSIFICATION_MASTER = "Master Dealer"
DEALER_CLASSIFICATIONS = [DEALER_CLASSIFICATION_STANDARD, DEALER_CLASSIFICATION_DEALER, DEALER_CLASSIFICATION_MASTER]

# BRD C.1.4 "confirmed initial bands": below 1L -> Standard Dealer, 1L-15L -> Dealer,
# 15L and above -> Master Dealer.
DEALER_MIN_TOTAL = 100_000
MASTER_DEALER_MIN_TOTAL = 1_500_000

DEFAULT_CLASSIFICATION_WINDOW_DAYS = 365


def _classification_window_days() -> int:
	return frappe.db.get_single_value("DMS Sales Settings", "dealer_classification_window_days") or DEFAULT_CLASSIFICATION_WINDOW_DAYS


def _confirmed_sales_value_by_customer(since) -> dict[str, float]:
	rows = frappe.db.sql(
		"""
		select customer, sum(base_grand_total) as total
		from `tabSales Order`
		where docstatus = 1 and transaction_date >= %s
		group by customer
		""",
		(since,),
		as_dict=True,
	)
	return {r.customer: r.total or 0 for r in rows}


def _classification_for_total(total: float) -> str:
	if total >= MASTER_DEALER_MIN_TOTAL:
		return DEALER_CLASSIFICATION_MASTER
	if total >= DEALER_MIN_TOTAL:
		return DEALER_CLASSIFICATION_DEALER
	return DEALER_CLASSIFICATION_STANDARD


def recompute_dealer_classifications() -> int:
	"""Daily scheduled job (BRD C.1.4): reclassifies every Customer from confirmed
	Sales Order value over the trailing window. Customers with zero qualifying
	sales are classified Standard Dealer, same as a brand-new dealer.

	custom_out_of_station (sales/setup.py) is a floor on top of the volume-based
	tier, not a separate rule: an out-of-station dealer whose sales alone would
	only earn Standard/Dealer still gets bumped to at least Master Dealer pricing
	(BRD C.1.4 — transport cost justifies it), but a genuinely high-volume
	out-of-station dealer is never bumped *down* to Master Dealer if their sales
	already earned it on their own — there is nothing above Master Dealer today,
	so in practice this only ever raises, never lowers.

	Returns the number of Customers whose classification actually changed, so
	callers/tests don't have to re-query to check whether anything happened."""
	since = add_days(today(), -_classification_window_days())
	totals = _confirmed_sales_value_by_customer(since)

	customers = frappe.get_all(
		"Customer", fields=["name", "custom_dealer_classification", "custom_out_of_station"]
	)
	changed = 0
	for customer in customers:
		new_classification = _classification_for_total(totals.get(customer.name, 0))
		if customer.custom_out_of_station and DEALER_CLASSIFICATIONS.index(
			new_classification
		) < DEALER_CLASSIFICATIONS.index(DEALER_CLASSIFICATION_MASTER):
			new_classification = DEALER_CLASSIFICATION_MASTER
		if new_classification != customer.custom_dealer_classification:
			frappe.db.set_value("Customer", customer.name, "custom_dealer_classification", new_classification)
			changed += 1
	return changed
