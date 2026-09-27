"""Order-channel auto-classification (BRD C.4.3). `custom_dealer_type` (Retail /
Bulk / Project) is a manually-set Customer category that, together with a Series'
bulk_qty_threshold (propagated onto Item by catalog.api.create_product), drives
the default `custom_order_channel` (Retail/Bulk/Project) on a new Quotation/Order.

The BRD's "audit-locked manual override" (also BRD C.11 trigger #6) is: a caller
that passes `channel` explicitly (including "Retail") always wins over
auto-classification -- but *making it stick* runs through gate_channel_override
below, not straight through. An already-authorized user's override still applies
immediately (they're the authority BRD C.11 requires), just logged as an
auto-approved Approval Request for audit; anyone else's override attempt is queued
as a Pending one instead, and the document isn't created until Management decides
it (see approvals.api.decide_approval / APPLIERS["Channel Override"]).
"""

import frappe
from frappe.utils import now_datetime

DEALER_TYPES = ["Retail", "Bulk", "Project"]
CHANNEL_OVERRIDE_AUTHORIZED_ROLES = {"DMS Management", "System Manager"}


def auto_classify_channel(dealer: str, lines: list[dict]) -> str:
	"""Bulk/Project dealers always default to their own type. Otherwise, Bulk if any
	line's qty meets its item's Series-sourced bulk_qty_threshold. An item with no
	threshold set (0/unset — no Series, or a Series that doesn't define one) never
	triggers the qty check on its own, so an untagged catalog defaults to Retail
	exactly as it always has."""
	dealer_type = frappe.db.get_value("Customer", dealer, "custom_dealer_type")
	if dealer_type in ("Bulk", "Project"):
		return dealer_type

	for line in lines:
		threshold = frappe.db.get_value("Item", line["item"], "custom_bulk_qty_threshold")
		if threshold and line["qty"] >= threshold:
			return "Bulk"

	return "Retail"


def gate_channel_override(
	*, requested_channel: str, default_channel: str, reference_doctype: str, create_fn, create_kwargs: dict
) -> dict:
	"""BRD C.11 trigger #6. `requested_channel` is what the caller resolved to
	(explicit or auto-classified); `default_channel` is always
	auto_classify_channel's own answer for the same dealer/lines, so the two only
	differ when a caller actually overrode it. No difference -> create_fn runs
	unconditionally, no approval-queue overhead for the common case.

	`create_fn(**create_kwargs)` must be the trigger's own "_unchecked" creation
	core (quotation_api._create_quotation / order_api._create_order) so that
	replaying it later from an approved request (approvals.api.APPLIERS) can't
	re-enter this gate and loop.
	"""
	if requested_channel == default_channel:
		return create_fn(**create_kwargs)

	from dms_erp.approvals.api import raise_approval_request

	authorized = bool(set(frappe.get_roles(frappe.session.user)) & CHANNEL_OVERRIDE_AUTHORIZED_ROLES)
	reason = (
		f"{frappe.session.user} set channel to {requested_channel} "
		f"(auto-classified: {default_channel})."
	)

	if authorized:
		result = create_fn(**create_kwargs)
		raise_approval_request(
			trigger_type="Channel Override",
			reason=reason,
			payload=create_kwargs,
			reference_doctype=reference_doctype,
			reference_name=result["id"],
			status="Approved",
			decided_by=frappe.session.user,
			decided_at=now_datetime(),
			decision_note="Auto-approved: raised by an already-authorized user.",
		)
		return result

	approval = raise_approval_request(
		trigger_type="Channel Override",
		reason=reason + " Needs Management approval before the document is created.",
		payload=create_kwargs,
		reference_doctype=reference_doctype,
	)
	return {"approvalRequired": True, "approval": approval}
