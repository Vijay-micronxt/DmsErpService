"""Credit-limit exceedance (BRD C.11 trigger #1). `Customer Credit Limit`
(ERPNext-native, keyed by company) is compared against a dealer's cumulative
submitted Sales Order value -- the same order-value approximation
dashboard.api._credit_exposure_alerts already uses for the Management
dashboard's own credit-exposure alert. This app posts no Sales Invoice, so
"committed order value" is an honest stand-in for true outstanding
receivables, not the real thing -- same caveat, same reason, as that alert.

Unlike order_channel.auto_classify_channel (a default a caller can silently
accept), there's no "default" outcome here to fall back to: either a new
order's value would push the dealer over their configured limit or it
wouldn't. A dealer with no credit limit configured (0/unset) is never gated
at all -- an untagged dealer behaves exactly as it always has.

Wired into three order-creating call sites, all of which produce a real
Sales Order: order_api.create_order (direct/Inquiry-sourced, staff), the same
core's use from comms.flow_api.create_dealer_opportunity (WhatsApp
order-placement, after a PO number) -- both an *estimate*, since real
per-line pricing/rounding happens later in _priced_order_line -- and
quotation_api.convert_to_order (the Quotation is already fully priced and
submitted, so its own `grand_total` is exact, not an estimate).

Deliberately NOT wired into sales.dealer_portal_api.convert_to_order (a
dealer's own self-service order via the separate dealer-portal web app) --
unlike the WhatsApp reply text (a plain string this app fully owns), that
endpoint's *response shape* is a JSON contract with a separate dealer-portal
frontend this session has no access to, so returning `{"approvalRequired":
True, ...}` there instead of an Order risks silently breaking a frontend
nothing in this session can also fix. That endpoint still gets
sales.utils.safe_customer_message's safety net against ERPNext's own native
credit-limit check leaking raw HTML/emails to a dealer -- it just doesn't get
the queue-for-approval treatment.
"""

import frappe

from dms_erp.warehouse.utils import default_company

CREDIT_LIMIT_AUTHORIZED_ROLES = {"DMS Management", "System Manager"}


def get_credit_limit(dealer: str) -> float:
	"""Mirrors ERPNext's own erpnext.selling.doctype.customer.customer.
	get_credit_limit fallback chain exactly (Customer -> Customer Group ->
	Company's own default). This used to check only the Customer-level row --
	a dealer whose limit actually came from their Customer Group or the
	Company default (not set directly on the Customer) read as "no limit
	configured" here and skipped this gate entirely, while ERPNext's own
	native credit check (which does fall back) still caught it at Sales
	Order submit time -- too late for the intended "queue for Management
	approval" flow, so the caller hit ERPNext's raw, unhandled "Credit Limit
	Crossed" error directly instead."""
	company = default_company()

	limit = frappe.db.get_value(
		"Customer Credit Limit",
		{"parent": dealer, "parenttype": "Customer", "company": company},
		"credit_limit",
	)
	if limit:
		return limit

	customer_group = frappe.get_cached_value("Customer", dealer, "customer_group")
	row = frappe.db.get_value(
		"Customer Credit Limit",
		{"parent": customer_group, "parenttype": "Customer Group", "company": company},
		["credit_limit", "bypass_credit_limit_check"],
		as_dict=True,
	)
	if row and not row.bypass_credit_limit_check:
		return row.credit_limit or 0

	return frappe.get_cached_value("Company", company, "credit_limit") or 0


def committed_order_value(dealer: str) -> float:
	"""Every submitted (docstatus=1) Sales Order's grand_total for this dealer
	in this app's default company -- the same figure dashboard.api.
	_credit_exposure_alerts already sums for its own alert."""
	row = frappe.db.sql(
		"""
		select coalesce(sum(grand_total), 0)
		from `tabSales Order`
		where customer = %(dealer)s and docstatus = 1 and company = %(company)s
		""",
		{"dealer": dealer, "company": default_company()},
	)
	return row[0][0] or 0


def gate_credit_limit(
	*,
	dealer: str,
	additional_value: float,
	reference_doctype: str,
	applier_action: str,
	create_fn,
	create_kwargs: dict,
) -> dict:
	"""BRD C.11 trigger #1. `additional_value` is the new order's own
	projected value; if committed_order_value(dealer) + additional_value would
	exceed the dealer's configured credit limit, this is an audit-locked
	override -- an already-authorized caller (DMS Management/System Manager)
	still applies immediately (now audit-logged), anyone else's attempt is
	queued as a Pending Approval Request instead of applied.

	`applier_action` distinguishes *how* the order gets created when this
	request is later approved -- see approvals.api._apply_credit_limit_exceeded,
	since unlike Channel Override/Discount Over Price List this trigger's
	reference_doctype ("Sales Order") doesn't by itself say whether to replay
	order_api._create_order or quotation_api._convert_to_order."""
	limit = get_credit_limit(dealer)
	if not limit:
		return create_fn(**create_kwargs)

	projected = committed_order_value(dealer) + additional_value
	if projected <= limit:
		return create_fn(**create_kwargs)

	from dms_erp.approvals.api import gate_document_creation

	return gate_document_creation(
		trigger_type="Credit Limit Exceeded",
		reference_doctype=reference_doctype,
		reason=(
			f"{frappe.session.user}'s order for {dealer} would bring committed "
			f"order value to {projected:.0f}, over the {limit:.0f} credit limit."
		),
		create_fn=create_fn,
		create_kwargs=create_kwargs,
		authorized_roles=CREDIT_LIMIT_AUTHORIZED_ROLES,
		applier_action=applier_action,
	)
