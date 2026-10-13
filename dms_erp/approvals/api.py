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

Wired so far: trigger #6 (Channel Override), trigger #5 (Amend Or Cancel
Submitted Document), trigger #4 (Discount Over Price List), and trigger #1
(Credit Limit Exceeded, sales.credit_limit.gate_credit_limit -- an already
real, existing signal: `Customer Credit Limit` vs a dealer's cumulative
submitted Sales Order value, the same one dashboard.api's own credit-exposure
alert already computes).

Trigger #3 (Pricing Override) is deliberately NOT separately wired -- in this
codebase's data model there's no transaction-level pricing lever apart from
the same per-line `discount_percentage` #4 already gates (BRD C.7.3's "any
transaction-level price or discount change" collapses to one field here); a
real #3 would need a genuine explicit-rate-override capability this API
doesn't have.

Trigger #2 (Overdue Outstanding) is also NOT wired -- unlike #3, this isn't a
missing field but a missing *subsystem*: "overdue" requires knowing which
invoices are past due and unpaid, and this app posts no Sales Invoice or
Payment Entry at all (see dashboard.api's own `outstandingReceivables: 0`
honesty). Building a real #2 means building AR tracking first, well beyond a
gate on an existing action.

See docs/BRD.md C.11 and this module's README for more.
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


def _users_with_any_role(roles: set[str]) -> list[str]:
	rows = frappe.get_all(
		"Has Role",
		filters={"role": ["in", list(roles)], "parenttype": "User"},
		pluck="parent",
		distinct=True,
	)
	if not rows:
		return []
	return frappe.get_all("User", filters={"name": ["in", rows], "enabled": 1}, pluck="name")


def _notify_pending(doc):
	"""BRD C.11 -- "owner notified even when a delegate acts": every Management/System
	Manager user is notified a request is waiting, not just whichever one happens to
	open the queue first, so a decision never lands with only one delegate ever having
	known about it."""
	subject = _("{0} needs your approval: {1}").format(doc.trigger_type, doc.reason)
	for user in _users_with_any_role(DECIDE_ROLES):
		if user == doc.requested_by:
			continue
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"document_type": "Approval Request",
				"document_name": doc.name,
				"subject": subject,
			}
		).insert(ignore_permissions=True)


def _notify_decision(doc):
	"""The other half of "owner notified even when a delegate acts": the original
	requester (the action's own owner) always hears the outcome, regardless of which
	specific Management user -- a delegate, not necessarily the requester's own usual
	approver -- actually made the call."""
	if not doc.requested_by:
		return
	subject = _("Your {0} request was {1}").format(doc.trigger_type, doc.status.lower())
	frappe.get_doc(
		{
			"doctype": "Notification Log",
			"for_user": doc.requested_by,
			"type": "Alert",
			"document_type": "Approval Request",
			"document_name": doc.name,
			"subject": subject,
			"from_user": doc.decided_by,
		}
	).insert(ignore_permissions=True)


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
	if doc.status == "Pending":
		_notify_pending(doc)
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
	_notify_decision(doc)

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
	document -- amend/cancel (#5) today; credit-limit/overdue (#1-#2) can reuse
	this once they're wired, since they're all "is this caller authorized to do
	this to a document that already exists" checks. Discount Over Price List
	(#4) and Channel Override (#6) instead gate a document's own *creation* --
	see gate_document_creation below -- since neither has a reference_name yet
	at gate time.

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


def gate_document_creation(
	*,
	trigger_type: str,
	reference_doctype: str,
	reason: str,
	create_fn,
	create_kwargs: dict,
	authorized_roles: set[str] | None = None,
	applier_action: str | None = None,
) -> dict:
	"""Shared gate for a BRD C.11 trigger that gates a document's own *creation*
	-- discount-over-price-list (#4) and credit-limit exceedance (#1) today,
	alongside channel override (#6, sales.order_channel.gate_channel_override,
	kept as its own function since it also has to compare the requested channel
	against auto_classify_channel's default before deciding whether this gate
	even applies). Unlike gate_authorized_action, there's no reference_name yet
	at gate time -- the document doesn't exist until `create_fn` actually runs.

	`authorized_roles` defaults to DECIDE_ROLES -- the same bar every one of the
	six triggers sets. A caller outside it gets `create_fn` queued as a Pending
	Approval Request instead of run; `create_fn(**create_kwargs)` never executes
	until Management approves it (see APPLIERS).

	`applier_action` is a discriminator stored alongside `create_kwargs` in the
	persisted payload (never passed to `create_fn` itself) -- for a trigger_type
	whose applier has to choose between several possible creation paths that
	all produce the same `reference_doctype` (credit-limit exceedance can come
	from either a direct order or a Quotation conversion, both "Sales Order"),
	this is how it knows which one to replay. Leave it unset when
	reference_doctype alone already disambiguates (Channel Override, Discount
	Over Price List: exactly one creation function per doctype)."""
	roles = authorized_roles or DECIDE_ROLES
	payload = dict(create_kwargs)
	if applier_action is not None:
		payload["action"] = applier_action

	if set(frappe.get_roles(frappe.session.user)) & roles:
		result = create_fn(**create_kwargs)
		raise_approval_request(
			trigger_type=trigger_type,
			reason=reason,
			payload=payload,
			reference_doctype=reference_doctype,
			reference_name=result["id"],
			status="Approved",
			decided_by=frappe.session.user,
			decided_at=now_datetime(),
			decision_note="Auto-approved: raised by an already-authorized user.",
		)
		return result

	approval = raise_approval_request(
		trigger_type=trigger_type,
		reason=reason + " Needs Management approval before the document is created.",
		payload=payload,
		reference_doctype=reference_doctype,
	)
	return {"approvalRequired": True, "approval": approval}


DEFAULT_DISCOUNT_APPROVAL_THRESHOLD_PCT = 0


def _discount_approval_threshold_pct() -> float:
	"""BRD C.7.3's own rule for Pacific is "even a one-rupee change" -- no
	threshold, hence the 0 default (same "get_single_value(...) or DEFAULT"
	pattern pricing.dealer_classification._classification_window_days already
	uses for its own BRD-left-open config knob). This app is white-labeled,
	though, so a different client's own business rule may be looser -- e.g.
	discounts up to 5% auto-apply, only above that needs Management approval.
	`DMS Sales Settings.discount_approval_threshold_pct` is that per-site knob;
	changing a client's threshold is a config change, not a code change."""
	return frappe.db.get_single_value("DMS Sales Settings", "discount_approval_threshold_pct") or DEFAULT_DISCOUNT_APPROVAL_THRESHOLD_PCT


def gate_discount_over_price_list(
	*, reference_doctype: str, lines: list[dict], create_fn, create_kwargs: dict
) -> dict:
	"""BRD C.11 trigger #4 / C.7.3: a transaction-level price or discount
	change is an override requiring approval once it's above
	`_discount_approval_threshold_pct()` -- 0 by default (Pacific's own rule is
	"even a one-rupee change", i.e. no threshold at all), but configurable per
	site for a different client. `discount_percentage` (0-100, set per line at
	create_quotation/create_order time) is this codebase's only
	transaction-level pricing lever, so any line above the threshold is the
	whole detection point -- a caller with no line over it never touches the
	approval queue at all.

	(Trigger #3, "any pricing override," is deliberately not wired separately --
	see approvals.api's own module docstring for why it collapses into this
	same check here.)"""
	threshold = _discount_approval_threshold_pct()
	if not any(float(line.get("discount_percentage") or 0) > threshold for line in lines):
		return create_fn(**create_kwargs)

	discounted_items = [
		line["item"] for line in lines if float(line.get("discount_percentage") or 0) > threshold
	]
	return gate_document_creation(
		trigger_type="Discount Over Price List",
		reference_doctype=reference_doctype,
		reason=f"{frappe.session.user} applied a discount on {', '.join(discounted_items)} (over the {threshold:.0f}% approval threshold).",
		create_fn=create_fn,
		create_kwargs=create_kwargs,
	)


def _apply_document_creation(doc) -> dict:
	"""Replays the original creation call now that Management has approved it --
	`doc.reference_doctype` was set at raise time (both gate_document_creation and
	gate_channel_override always know which doctype they're gating before the
	document exists), so no dispatch table keyed on trigger_type+doctype is
	needed beyond this if/elif. Shared by every trigger that gates a creation
	(Channel Override, Discount Over Price List)."""
	payload = json.loads(doc.payload or "{}")
	if doc.reference_doctype == "Quotation":
		from dms_erp.sales.quotation_api import _create_quotation as create_fn
	elif doc.reference_doctype == "Sales Order":
		from dms_erp.sales.order_api import _create_order as create_fn
	else:
		frappe.throw(_("Unknown reference doctype for {0}: {1}").format(doc.trigger_type, doc.reference_doctype))
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


def _apply_credit_limit_exceeded(doc) -> dict:
	"""Replays the original order-creating call now that Management has
	approved it. Unlike Channel Override/Discount Over Price List (exactly one
	creation function per reference_doctype), this trigger's `reference_doctype`
	is always "Sales Order" whether the order came from order_api.create_order
	(direct/Inquiry-sourced) or quotation_api.convert_to_order (a Quotation
	conversion) -- `payload["action"]` (set via gate_credit_limit's
	`applier_action`) says which one to replay."""
	payload = dict(json.loads(doc.payload or "{}"))
	action = payload.pop("action", None)

	if action == "create_order":
		from dms_erp.sales.order_api import _create_order as create_fn
	elif action == "convert_to_order":
		from dms_erp.sales.quotation_api import _convert_to_order as create_fn
	else:
		frappe.throw(_("Unknown credit-limit action: {0}").format(action), frappe.ValidationError)

	result = create_fn(**payload)
	return {"doctype": "Sales Order", "name": result["id"]}


APPLIERS = {
	"Channel Override": _apply_document_creation,
	"Discount Over Price List": _apply_document_creation,
	"Amend Or Cancel Submitted Document": _apply_amend_or_cancel,
	"Credit Limit Exceeded": _apply_credit_limit_exceeded,
}
