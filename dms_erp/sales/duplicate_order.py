"""Duplicate-order hold (client request, 12 Nov 2026 -- not one of BRD C.11's own
six triggers, but gated the same way: an already-authorized caller's order still
applies immediately, logged; anyone else's is queued as a Pending Approval
Request instead of created outright).

Narrower than BRD C.2.4's own duplicate-inquiry window (sales.utils.
find_open_duplicate_inquiries, 7 days, always just a hint a caller can ignore):
this only fires when the dealer's own inquiry for the same item in the last 24
hours already became a real Sales Order (sales.utils.
recent_converted_inquiry_for) -- a second one that soon is far more likely a
genuine double-order than a dealer legitimately asking about the same item
twice, which is exactly what the plain 24-hour warning (recent_inquiry_for,
already wired into the WhatsApp item-lookup reply and the dealer portal's own
"Add to enquiry" check) already covers without blocking anything.

Wired into the two order-creating call sites that share the same underlying
_create_order core: order_api.create_order (staff, direct/Inquiry-sourced) and
comms.flow_api.create_dealer_opportunity (WhatsApp, after a PO number).
Deliberately NOT wired into sales.dealer_portal_api.convert_to_order yet --
same reasoning sales.credit_limit's own module docstring already gives for
leaving that endpoint out: its response shape is a JSON contract with the
separate dealer-portal frontend, and returning {"approvalRequired": True, ...}
there instead of an Order needs a matching change on that side first.
"""

import frappe


def gate_duplicate_order(
	*,
	dealer: str,
	lines: list[dict],
	create_fn,
	create_kwargs: dict,
) -> dict:
	"""Checked per line (an order can carry more than one item -- order_api.
	create_order's own multi-line case; WhatsApp's create_dealer_opportunity
	only ever builds one-line orders, but this still has to hold correctly for
	the general case) -- the whole order holds if ANY line matches, not just the
	first. `create_fn`/`create_kwargs` must be the unchecked order-creation core
	(order_api._create_order) so replaying it later from an approved request
	(approvals.api.APPLIERS["Duplicate Order"] -- shared with Channel Override/
	Discount Over Price List, since this always targets "Sales Order" the same
	single way) can't re-enter this gate and loop."""
	from dms_erp.sales.utils import recent_converted_inquiry_for

	prior = None
	for line in lines:
		prior = recent_converted_inquiry_for(dealer, line["item"])
		if prior:
			break
	if not prior:
		return create_fn(**create_kwargs)

	from dms_erp.approvals.api import gate_document_creation

	return gate_document_creation(
		trigger_type="Duplicate Order",
		reference_doctype="Sales Order",
		reason=(
			f"{frappe.session.user}'s new order for {dealer} comes within 24 hours of {prior.name}, "
			f"already converted to {prior.linked_sales_order} for the same item -- needs Management "
			f"sign-off before a second order for it goes out."
		),
		create_fn=create_fn,
		create_kwargs=create_kwargs,
	)
