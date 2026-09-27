"""Approval Request (BRD C.11) -- a generic queue/ledger for the six audit-locked
override triggers, rather than Frappe's native Workflow doctype: each trigger needs
a computed business gate (credit-limit math, price comparisons, channel
auto-classification) that's simpler to express as a Python guard than as a
declarative Workflow transition condition. One doctype, three whitelisted
endpoints, and a small per-trigger-type "applier" registry -- wiring up trigger #N
later means adding one entry to APPLIERS and one call site, not a new doctype or a
new set of endpoints.

Only `DMS Management`/`System Manager` may decide (approve/reject) a request --
the same "authorized user" bar BRD C.11 sets for every one of the six triggers.
Raising a request has no role gate of its own: each trigger's own call site (e.g.
sales.order_channel.gate_channel_override, called from quotation_api.create_quotation
and order_api.create_order) already gates who can attempt the underlying action in
the first place.

Wired so far: trigger #6 (Channel Override) and trigger #5 (Amend Or Cancel
Submitted Document). The other four reserved `trigger_type` values are designed
but not yet raised by any code path -- see docs/BRD.md C.11 and this module's
README for the planned detection points.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from dms_erp.pagination import clamp

DECIDE_ROLES = {"DMS Management", "System Manager"}


def _assert_can_decide():
	if not set(frappe.get_roles(frappe.session.user)) & DECIDE_ROLES:
		frappe.throw(_("Only Management can decide approval requests."), frappe.PermissionError)


def _serialize(doc) -> dict:
	return {
		"id": doc.name,
		"triggerType": doc.trigger_type,
		"status": doc.status,
		"referenceDoctype": doc.reference_doctype,
		"referenceName": doc.reference_name,
		"requestedBy": doc.requested_by,
		"requestedAt": doc.requested_at,
		"reason": doc.reason,
		"payload": json.loads(doc.payload or "{}"),
		"decidedBy": doc.decided_by,
		"decidedAt": doc.decided_at,
		"decisionNote": doc.decision_note,
	}


def raise_approval_request(
	trigger_type: str,
	reason: str,
	payload: dict,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	status: str = "Pending",
	decided_by: str | None = None,
	decided_at=None,
	decision_note: str | None = None,
) -> dict:
	"""Called from a trigger's own call site -- never whitelisted directly, since
	there's no legitimate reason for a client to raise one out of band from the
	action that needs it. `status`/`decided_*` let an already-authorized requester's
	own action be logged as auto-approved (still an audit trail entry, just no
	queue wait) instead of always landing Pending."""
	doc = frappe.get_doc(
		{
			"doctype": "Approval Request",
			"trigger_type": trigger_type,
			"status": status,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"requested_by": frappe.session.user,
			"requested_at": now_datetime(),
			"reason": reason,
			"payload": json.dumps(payload, default=str),
			"decided_by": decided_by,
			"decided_at": decided_at,
			"decision_note": decision_note,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist(methods=["GET"])
def list_pending_approvals(
	trigger_type: str | None = None, status: str = "Pending", limit: int = 20, offset: int = 0
):
	_assert_can_decide()
	limit, offset = clamp(limit, offset)
	filters = {}
	if status:
		filters["status"] = status
	if trigger_type:
		filters["trigger_type"] = trigger_type
	total = frappe.db.count("Approval Request", filters=filters)
	names = frappe.get_all(
		"Approval Request",
		filters=filters,
		pluck="name",
		order_by="requested_at desc",
		limit_start=offset,
		limit_page_length=limit,
	)
	return {
		"items": [_serialize(frappe.get_doc("Approval Request", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_approval(name: str):
	_assert_can_decide()
	return _serialize(frappe.get_doc("Approval Request", name))


@frappe.whitelist(methods=["POST"])
def decide_approval(name: str, decision: str, note: str | None = None):
	_assert_can_decide()
	if decision not in ("Approved", "Rejected"):
		frappe.throw(_("Decision must be Approved or Rejected."), frappe.ValidationError)

	doc = frappe.get_doc("Approval Request", name)
	if doc.status != "Pending":
		frappe.throw(_("This request has already been decided."), frappe.ValidationError)

	doc.status = decision
	doc.decided_by = frappe.session.user
	doc.decided_at = now_datetime()
	doc.decision_note = note
	doc.save(ignore_permissions=True)

	if decision == "Approved":
		applier = APPLIERS.get(doc.trigger_type)
		if applier:
			result = applier(doc)
			if result:
				doc.reference_doctype = result["doctype"]
				doc.reference_name = result["name"]
				doc.save(ignore_permissions=True)

	return _serialize(doc)


def gate_authorized_action(
	*,
	trigger_type: str,
	reference_doctype: str,
	reference_name: str,
	reason: str,
	action_fn,
	action_kwargs: dict,
	authorized_roles: set[str] | None = None,
	applier_action: str | None = None,
) -> dict:
	"""Shared gate for a BRD C.11 trigger that acts on an *already-existing*
	document -- amend/cancel (#5) today; credit-limit/overdue/pricing/discount
	(#1-#4) can reuse this once they're wired, since they're all "is this caller
	authorized to do this to a document that already exists" checks, unlike
	gate_channel_override (order_channel.py), which gates a document's own
	creation and so never has a reference_name yet at gate time.

	`authorized_roles` defaults to DECIDE_ROLES -- the same bar every one of the
	six triggers sets. A caller outside it gets `action_fn` queued as a Pending
	Approval Request instead of run; `action_fn(**action_kwargs)` never executes
	until Management approves it (see APPLIERS).

	`applier_action` is a discriminator stored alongside `action_kwargs` in the
	persisted payload (never passed to `action_fn` itself) -- for a trigger_type
	whose applier has to choose between several possible actions on the same
	reference_doctype (e.g. Quotation's add/remove/update-qty), this is how it
	knows which one to replay. Leave it unset when reference_doctype alone
	already disambiguates the action (one action per doctype)."""
	roles = authorized_roles or DECIDE_ROLES
	payload = dict(action_kwargs)
	if applier_action is not None:
		payload["action"] = applier_action

	if set(frappe.get_roles(frappe.session.user)) & roles:
		result = action_fn(**action_kwargs)
		raise_approval_request(
			trigger_type=trigger_type,
			reason=reason,
			payload=payload,
			reference_doctype=reference_doctype,
			# The action may itself replace the document (Quotation's amend cycle
			# names the new copy differently from `reference_name` above, which is
			# only ever the pre-action name) -- result["id"] is the one that's
			# actually live afterwards, so that's what the audit trail should point at.
			reference_name=result.get("id", reference_name),
			status="Approved",
			decided_by=frappe.session.user,
			decided_at=now_datetime(),
			decision_note="Auto-approved: raised by an already-authorized user.",
		)
		return result

	approval = raise_approval_request(
		trigger_type=trigger_type,
		reason=reason + " Needs Management approval before this takes effect.",
		payload=payload,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
	)
	return {"approvalRequired": True, "approval": approval}


def _apply_channel_override(doc) -> dict:
	"""Replays the original creation call now that Management has approved it --
	`doc.reference_doctype` was set at raise time (gate_channel_override always
	knows which doctype it's gating before the document exists), so no dispatch
	table keyed on trigger_type+doctype is needed beyond this if/elif."""
	payload = json.loads(doc.payload or "{}")
	if doc.reference_doctype == "Quotation":
		from dms_erp.sales.quotation_api import _create_quotation as create_fn
	elif doc.reference_doctype == "Sales Order":
		from dms_erp.sales.order_api import _create_order as create_fn
	else:
		frappe.throw(_("Unknown reference doctype for Channel Override: {0}").format(doc.reference_doctype))
	result = create_fn(**payload)
	return {"doctype": doc.reference_doctype, "name": result["id"]}


def _apply_amend_or_cancel(doc) -> dict:
	"""Replays the original amend/cancel call now that Management has approved
	it. `doc.reference_doctype`/`reference_name` were both already known at raise
	time (unlike Channel Override, this trigger always acts on a document that
	already exists), so the payload only needs to say *which* action on that
	document -- `payload["action"]` -- plus that action's own kwargs."""
	payload = dict(json.loads(doc.payload or "{}"))
	action = payload.pop("action", None)

	if doc.reference_doctype == "Quotation":
		from dms_erp.sales.quotation_api import AMEND_ACTIONS

		action_fn = AMEND_ACTIONS.get(action)
	elif doc.reference_doctype == "Sales Order":
		from dms_erp.sales.order_api import _cancel_order_action

		action_fn = _cancel_order_action if action == "cancel" else None
	else:
		action_fn = None

	if action_fn is None:
		frappe.throw(
			_("Unknown amend/cancel action {0} for {1}.").format(action, doc.reference_doctype),
			frappe.ValidationError,
		)

	result = action_fn(**payload)
	return {"doctype": doc.reference_doctype, "name": result["id"]}


APPLIERS = {
	"Channel Override": _apply_channel_override,
	"Amend Or Cancel Submitted Document": _apply_amend_or_cancel,
}
