"""BRD C.3.4 -- which orders owe an advance, and whether they've paid it.

The gate used to be a bare per-order tick that applied to *every* order, so a credit
dealer was blocked exactly like a cash dealer. The BRD ties the condition to the
dealer's ERPNext Payment Terms Template instead; this module is the one place that
turns "template + order total + money received" into a verdict, so the dispatch gate
(order_api), the order screen and the pending report cannot disagree.

Advance % lives on a custom field of the Payment Terms Template rather than being
inferred from its rows: a template's due-date rows describe *invoice* credit, and
guessing "first row has 0 credit days means advance" would silently gate dealers
whose Desk-built template merely has an immediate-due first row.

"Received" is Sales Order.advance_paid -- the native field ERPNext keeps current from
Payment Entries that reference the order -- so recording money the normal ERPNext way
opens the gate with no extra step. Management's manual confirmation stays as an
override (bank checked off-system, VALS not integrated), never as the only path.
"""

import frappe
from frappe.utils import flt


def template_for(customer: str | None, order_template: str | None = None) -> str | None:
	"""The order's own snapshot wins (that is what it was priced on); the dealer's
	current template only fills in for orders created without one."""
	if order_template:
		return order_template
	if not customer:
		return None
	return frappe.db.get_value("Customer", customer, "payment_terms") or None


def advance_pct_of(template: str | None) -> float:
	if not template:
		return 0.0
	return flt(frappe.db.get_value("Payment Terms Template", template, "custom_advance_pct"))


def advance_status(
	customer: str | None,
	grand_total: float,
	advance_paid: float,
	confirmed: bool,
	order_template: str | None = None,
) -> dict:
	template = template_for(customer, order_template)
	pct = advance_pct_of(template)
	required = round(flt(grand_total) * pct / 100, 2)
	received = flt(advance_paid)
	applies = pct > 0
	paid_enough = received + 0.005 >= required
	return {
		"applies": applies,
		"template": template,
		"percent": pct,
		"required": required,
		"received": received,
		"balance": max(round(required - received, 2), 0.0),
		"confirmedManually": bool(confirmed),
		# No advance condition -> nothing to wait for. Otherwise either real money
		# covers it or Management has signed it off by hand.
		"satisfied": (not applies) or paid_enough or bool(confirmed),
	}


def advance_status_for_order(doc) -> dict:
	return advance_status(
		doc.customer,
		doc.grand_total,
		doc.get("advance_paid"),
		bool(doc.get("custom_advance_confirmed")),
		doc.get("payment_terms_template"),
	)
