# Orientation

`dms_erp` is a custom Frappe app (installed into an existing ERPNext site)
backing Pacific Inc's internal staff ops system — auth, catalog, pricing,
warehouse, purchase, sales, finance, comms, dashboard, approvals. It serves a
separate React/TanStack SPA (`pacific-tileflow`, a different repo, not in
this session's scope) over a pure JSON API.

Read in this order before making changes:

1. **`ARCHITECTURE.md`** — the narrative: how the system is shaped and why,
   what's deliberately not built. Start here if you're new to the codebase.
   A published, shareable version lives at the Artifact link in its own
   header — update the `.md` file first and republish that artifact, not
   the reverse.
2. **`README.md`** — the operational reference: install steps,
   `site_config.json` keys, full endpoint list.
3. **Each module's own `dms_erp/<module>/README.md`** (`approvals`,
   `catalog`, `comms`, `dashboard`, `finance`, `pricing`, `purchase`,
   `reports`, `sales`, `warehouse`) — the why behind that module's specific
   design choices. Read the relevant one before touching a module; they
   record traps already hit once (e.g. `sales/credit_limit.py`'s docstring
   on why `dealer_portal_api.convert_to_order` is deliberately *not* gated
   the same way `order_api.create_order` is).
4. **`docs/BRD.md`** / **`docs/BRD_v2.2.pdf`** — the actual business
   requirements doc everything traces back to. When a feature request
   references a BRD section (e.g. "C.11", "C.7.3"), check the literal
   wording there before guessing intent.
5. **`docs/qa-usecase-testcase-guide.md`** — the living QA test-case guide.
   If you build or change user-facing behavior, add/update the relevant test
   cases here in the same change, not as a follow-up.
6. **`API_REFERENCE.md`** — full endpoint signatures, if you need the exact
   shape of a call without reading the handler.

## Conventions this codebase actually follows

- **Indentation is tabs**, not spaces (matches Frappe/ERPNext core style).
- **Docstrings explain *why*, not *what*.** A module or function docstring
  here typically justifies a design decision, names the alternative that was
  rejected and why, or documents a real bug/report that shaped the code —
  not a restatement of the signature. Match that register in new code;
  don't write comments a reader could infer from the code itself.
- **"Unguarded core" + "whitelisted wrapper" split**, used throughout
  `sales/*_api.py`: the whitelisted endpoint does auth/role checks and any
  approval gating, then calls a private `_verb_noun` function that takes only
  simple/serializable args and does the actual work. This exists so the
  `approvals` system can replay an action later from a JSON-serialized
  payload (see `dms_erp/approvals/README.md`) — new mutating endpoints
  likely to need approval-gating later should follow the same split even if
  they aren't gated yet.
- **Honest stubs over fabricated numbers.** Where a real subsystem doesn't
  exist yet (e.g. no Sales Invoice/AR ledger anywhere in this app), the code
  returns an honest zero/empty/"not built" rather than a plausible-looking
  fake value. See `dashboard.api`'s `outstandingReceivables: 0` and
  `approvals/README.md`'s note on BRD C.11 trigger #2 for the pattern to
  follow if you hit the same kind of gap.
- **Dealer/customer-facing text is a security boundary.** Anything returned
  to a dealer (WhatsApp replies, dealer-portal API responses) must never
  forward raw ERPNext/Frappe exception text verbatim — `sales.utils.
  safe_customer_message` is the enforced choke point; route any new
  dealer-facing error path through it. (This was a real production leak,
  not a hypothetical — see that function's docstring.)
- **White-label config lives in `DMS Sales Settings`** (a Single doctype),
  read via `frappe.db.get_single_value(...) or DEFAULT`, never hardcoded —
  this app is white-labeled across clients with different business rules
  (e.g. `discount_approval_threshold_pct`). It's Desk-only by design; the
  React frontend does not expose a settings screen for it.

## Testing

Per-module `test_*.py` files use `FrappeTestCase` and need a real Frappe
`bench` site to execute — **this environment does not have one**, so tests
cannot actually be run here. The practical local verification loop is:

```bash
python3 -m py_compile <changed files>
ruff check <changed files>
```

Treat that as necessary, not sufficient — reason carefully through test
logic by hand (as the existing tests already do), since nothing here proves
a `FrappeTestCase` actually passes.

## Git

Single feature branch: `claude/frappe-staff-auth-ilpczr`. Commit messages
here favor explaining *why* a change was made over restating the diff.
