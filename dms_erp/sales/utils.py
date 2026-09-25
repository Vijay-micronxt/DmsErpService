"""Shared helpers used across the sales module: the duplicate-inquiry predicate
(create_inquiry's same-moment check and reports.duplicate_inquiry_report's
after-the-fact list agree on what "still open" and "the same dealer+item" mean,
and on the default window, instead of drifting apart as two copies of the same
logic), and GST/tax template application shared by order_api.create_order and
quotation_api.create_quotation.

Tax handling here deliberately does nothing ERPNext doesn't already do:
`apply_tax_template` only points a document at an existing Sales Taxes and
Charges Template and copies its rows across; it never computes a rate itself.
Standard ERPNext math (calculate_taxes_and_totals, run automatically as part
of doc.insert()/doc.save()) does the actual qty*rate*tax_rate work — this app
never re-derives a GST amount by hand. If a site hasn't configured a template,
`taxes_and_charges` stays unset and the document is simply untaxed, same as
any other ERPNext site with no GST setup; nothing here assumes one exists.
"""

import frappe
from frappe.utils import add_days, getdate

from dms_erp.warehouse.utils import default_company

DUPLICATE_INQUIRY_WINDOW_DAYS = 7

# Same "not yet resolved" set duplicate_inquiry_report already used before this
# existed — a Converted/Rejected/Mapped/Closed inquiry is history, not a live
# duplicate risk.
CLOSED_INQUIRY_STATUSES = {"Converted to Order", "Rejected", "Mapped to PO", "Closed"}


def find_open_duplicate_inquiries(
	dealer: str, item: str, window_days: int = DUPLICATE_INQUIRY_WINDOW_DAYS, as_of=None
) -> list[dict]:
	"""BRD C.2.4 — still-open inquiries for the same dealer+item, logged within
	`window_days` before `as_of` (today by default). Newest first. Used by
	create_inquiry (checked against the moment of creation, before the new
	inquiry itself exists to match against) as a same-moment prompt, not a hard
	block — the caller decides whether to warn or let it through."""
	from dms_erp.sales.inquiry_api import list_all_inquiries

	as_of = getdate(as_of) if as_of else getdate()
	since = add_days(as_of, -window_days)
	matches = [
		i
		for i in list_all_inquiries(dealer=dealer)
		if i["productId"] == item and i["status"] not in CLOSED_INQUIRY_STATUSES and i["date"] and since <= getdate(i["date"]) <= as_of
	]
	matches.sort(key=lambda i: i["date"], reverse=True)
	return matches


@frappe.whitelist(methods=["GET"])
def list_tax_templates() -> list[dict]:
	"""Whatever Sales Taxes and Charges Templates the site's own GST/Chart of
	Accounts setup already has configured -- a thin passthrough, not a list this
	app curates. Empty when the site has none configured; that's a valid state,
	not an error, since GST setup is the site admin's responsibility, not this
	app's."""
	rows = frappe.get_all(
		"Sales Taxes and Charges Template",
		filters={"company": default_company(), "disabled": 0},
		fields=["name", "title", "is_default"],
		order_by="is_default desc, title asc",
	)
	return [{"id": r.name, "label": r.title or r.name, "isDefault": bool(r.is_default)} for r in rows]


def apply_tax_template(doc, taxes_and_charges: str | None) -> None:
	"""Points `doc` (an unsaved Sales Order or Quotation) at an existing Sales
	Taxes and Charges Template and copies its rows across. Copies explicitly
	rather than leaving it to ERPNext's own on-save auto-population (which would
	also happen, harmlessly, since it only fires when `taxes` is still empty) so
	the tax rows are visible immediately on the object this function returns,
	not only after a fresh reload. Never computes a rate itself -- every number
	is exactly what the site's own template says; calculate_taxes_and_totals
	(run automatically as part of doc.insert()) does the actual qty*rate*tax_rate
	math, same as it would for any Sales Order/Quotation on the site."""
	if not taxes_and_charges:
		return
	doc.taxes_and_charges = taxes_and_charges
	if doc.taxes:
		return
	template = frappe.get_doc("Sales Taxes and Charges Template", taxes_and_charges)
	for row in template.taxes:
		doc.append(
			"taxes",
			{
				"charge_type": row.charge_type,
				"account_head": row.account_head,
				"description": row.description,
				"rate": row.rate,
				"included_in_print_rate": row.included_in_print_rate,
				"cost_center": row.cost_center,
			},
		)
