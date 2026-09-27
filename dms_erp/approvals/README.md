# Approvals

BRD §C.11: six "audit-locked, authorized user only" override triggers. A generic
queue/ledger doctype (`Approval Request`) plus three whitelisted endpoints, rather
than Frappe's native Workflow doctype -- each trigger needs a computed business
gate (credit-limit math, price comparisons, channel auto-classification) that's
simpler to express as a Python guard than as a declarative Workflow transition
condition. See `api.py`'s own module docstring for the exact contract.

## The six triggers (BRD C.11)

1. Credit-limit exceedance
2. Overdue outstanding
3. Any pricing override (even ₹1)
4. Any discount over the default price list
5. Amendment or cancellation of a submitted sales/purchase document
6. Retail-vs-bulk classification override

Only **#6 (Channel Override)** is wired today: `sales.order_channel.
gate_channel_override`, called from `quotation_api.create_quotation` and
`order_api.create_order` whenever an explicit `channel` differs from
`auto_classify_channel`'s own answer. The other five are reserved
`trigger_type` values on the doctype, not yet raised by any code path.

## How a trigger plugs in

1. At the call site that can produce the override, detect it and either:
   - run the action immediately when the caller already holds the trigger's
     authorized role, logging an auto-approved `Approval Request` for audit
     (`raise_approval_request(..., status="Approved", decided_by=..., decided_at=...)`), or
   - queue a Pending one instead of running the action
     (`raise_approval_request(...)`, then return `{"approvalRequired": True,
     "approval": {...}}` to the caller rather than the normal result).
2. Add one entry to `APPLIERS` keyed by `trigger_type`: a function that takes the
   decided `Approval Request` doc, replays the original call from `doc.payload`,
   and returns `{"doctype": ..., "name": ...}` so `decide_approval` can stamp
   `reference_doctype`/`reference_name` once the action actually happens.

No new doctype, no new endpoints -- `list_pending_approvals`/`get_approval`/
`decide_approval` and the `Approval Request` list view work for every trigger
type as soon as it's raised.

## Who can do what

- Raising a request has no role gate of its own -- each trigger's own call site
  already gates who can attempt the underlying action (e.g.
  `quotation_api.QUOTATION_WRITE_ROLES`).
- Deciding one (`decide_approval`) is `DMS Management`/`System Manager` only
  (`DECIDE_ROLES`) -- the same "authorized user" bar every one of the six
  triggers sets.
