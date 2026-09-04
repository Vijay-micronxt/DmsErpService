"""Finance-side reports (BRD "Reports and Dashboards"). Read-only over the
existing claims/unloading listings — see sales_reports.py's module docstring for
the report-vs-dashboard distinction this whole `reports` module follows.
"""

import frappe
from frappe.utils import getdate

from dms_erp.finance import claims_api, unloading_api


def _in_range(d, from_date, to_date) -> bool:
	if not d:
		return from_date is None and to_date is None
	d = getdate(d)
	if from_date and d < getdate(from_date):
		return False
	if to_date and d > getdate(to_date):
		return False
	return True


@frappe.whitelist(methods=["GET"])
def damage_and_insurance_report(status: str | None = None, insurer: str | None = None, from_date=None, to_date=None):
	"""The BRD's "Damage and insurance report" — every claim, filterable by insurer
	and filed-date range on top of claims_api.list_claims's own status filter."""
	rows = [
		r
		for r in claims_api.list_all_claims(status=status)
		if (not insurer or r["insurer"] == insurer) and _in_range(r["filedAt"], from_date, to_date)
	]
	return {
		"rows": rows,
		"summary": {
			"count": len(rows),
			"totalClaimed": sum(r["claimAmount"] or 0 for r in rows),
			"totalSettled": sum(r["settledAmount"] or 0 for r in rows if r["status"] == "Settled"),
		},
	}


@frappe.whitelist(methods=["GET"])
def claimable_value_report():
	"""The BRD's "Claimable value report" — claim_summary's totals, broken down by
	insurer (claim_summary itself is a single fleet-wide aggregate; this report is
	the row-level version a report screen wants)."""
	summary = claims_api.claim_summary()

	by_insurer: dict[str, dict] = {}
	for c in claims_api.list_all_claims():
		bucket = by_insurer.setdefault(c["insurer"], {"insurer": c["insurer"], "receivable": 0, "settled": 0})
		if c["status"] in ("Filed", "Approved"):
			bucket["receivable"] += c["claimAmount"] or 0
		elif c["status"] == "Settled":
			bucket["settled"] += c["settledAmount"] or 0

	return {**summary, "byInsurer": sorted(by_insurer.values(), key=lambda b: b["receivable"] + b["settled"], reverse=True)}


@frappe.whitelist(methods=["GET"])
def unloading_payment_report(status: str | None = None, contractor: str | None = None, from_date=None, to_date=None):
	"""The BRD's "Unloading payment report" — every charge, filterable by contractor
	and recorded-date range on top of unloading_api.list_charges's own status filter."""
	rows = [
		r
		for r in unloading_api.list_all_charges(status=status)
		if (not contractor or r["contractor"] == contractor) and _in_range(r["recordedAt"], from_date, to_date)
	]
	return {
		"rows": rows,
		"summary": {
			"count": len(rows),
			"totalPending": sum(r["chargeAmount"] for r in rows if r["status"] == "Pending"),
			"totalPaid": sum(r["chargeAmount"] for r in rows if r["status"] == "Paid"),
		},
	}
