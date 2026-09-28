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

Three are wired today:

- **#6 (Channel Override)**: `sales.order_channel.gate_channel_override`, called
  from `quotation_api.create_quotation` and `order_api.create_order` whenever an
  explicit `channel` differs from `auto_classify_channel`'s own answer. Gates a
  document's own *creation* (`approvals.api.gate_document_creation`) -- there's
  no `reference_name` yet at gate time.
- **#5 (Amend Or Cancel Submitted Document)**: `approvals.api.
  gate_authorized_action`, called from `quotation_api.add_quotation_line`/
  `remove_quotation_line`/`update_quotation_line_qty` (the amend cycle) and
  `order_api.advance_order_stage`'s move to "Cancelled". Gates an action on a
  document that *already exists*, so `reference_name` is always known upfront
  (though the amend cycle replaces the document with a new one on success --
  `gate_authorized_action` re-points `reference_name` at whatever the action
  actually returns, not the pre-action name).
- **#4 (Discount Over Price List)**: `approvals.api.
  gate_discount_over_price_list`, called from `quotation_api.create_quotation`
  and `order_api.create_order` whenever any line carries a nonzero
  `discount_percentage`. BRD C.7.3 sets no threshold ("even a one-rupee
  change"), so any discount at all triggers this -- gates creation, same as #6,
  and the two compose: an authorized caller doing both in one call gets an
  audit record for each (see quotation_api.create_quotation's nested
  `_create_after_discount_gate`).

**#3 (Pricing Override) is deliberately not wired separately.** BRD C.7.3 talks
about "any transaction-level price or discount change" as one idea, but this
codebase only has one transaction-level pricing lever -- `discount_percentage`
-- already covered by #4. There's no explicit-rate-override field a caller can
set instead; building a real, distinct #3 means adding that capability first,
not just another gate.

The remaining two (#1 credit-limit exceedance, #2 overdue outstanding) are
reserved `trigger_type` values on the doctype, not yet raised by any code path.

## How a trigger plugs in

1. At the call site that can produce the override, detect it and either:
   - run the action immediately when the caller already holds the trigger's
     authorized role, logging an auto-approved `Approval Request` for audit
     (`raise_approval_request(..., status="Approved", decided_by=..., decided_at=...)`
     -- or just call `gate_authorized_action` (gates an action on a document
     that already exists) or `gate_document_creation` (gates a document's own
     creation), which already do this), or
   - queue a Pending one instead of running the action
     (`raise_approval_request(...)`, then return `{"approvalRequired": True,
     "approval": {...}}` to the caller rather than the normal result).
2. Add one entry to `APPLIERS` keyed by `trigger_type`: a function that takes the
   decided `Approval Request` doc, replays the original call from `doc.payload`,
   and returns `{"doctype": ..., "name": ...}` so `decide_approval` can stamp
   `reference_doctype`/`reference_name` once the action actually happens. When a
   `trigger_type` covers more than one possible action on the same
   `reference_doctype` (trigger #5's three Quotation actions), pass
   `gate_authorized_action`'s `applier_action` to record which one to replay --
   see `_apply_amend_or_cancel`.

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
