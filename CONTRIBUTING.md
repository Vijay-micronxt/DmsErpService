# Contributing

There is no CI pipeline configured on this repo yet (no `.github/workflows`)
— nothing enforces lint or tests on a PR automatically. Until that changes,
treat the checks in this doc as mandatory before you open one, not optional.

## Before you start

1. Read **`CLAUDE.md`** — the conventions this codebase actually follows
   (indentation, docstring style, the unguarded-core/whitelisted-wrapper
   split, the dealer-facing error boundary, white-label config).
2. Read **`ARCHITECTURE.md`** for how the system is shaped, and the
   relevant `dms_erp/<module>/README.md` for the module you're touching —
   these record design decisions and traps already hit once; don't
   re-litigate or re-break them unknowingly.
3. If the change traces back to a BRD requirement, check the literal
   wording in `docs/BRD.md` / `docs/BRD_v2.2.pdf` rather than guessing
   intent from a section number alone — ambiguous BRD wording has caused
   rework before (e.g. the discount-approval threshold, BRD C.7.3).
4. If you're not sure whether a change is in scope, small enough, or the
   right approach, ask before building — a wrong guess on a gated/audited
   flow (approvals, credit limits, pricing) is expensive to unwind.

## Branching

One topic per branch, one topic per PR. Give the branch a name that
describes the change (`fix-...`, `add-...`), not a generic placeholder.
Don't stack unrelated changes onto an existing open branch/PR.

## Making a change

- Follow the existing module's pattern rather than introducing a new one.
  In particular: a whitelisted endpoint under `sales/*_api.py` that mutates
  data should stay split into a thin `@frappe.whitelist` wrapper (auth/role
  checks, approval gating) and a private `_verb_noun` core that takes only
  simple/serializable args — this is what lets `dms_erp/approvals` replay
  an action later. See `dms_erp/approvals/README.md`.
- Never forward a raw ERPNext/Frappe exception to a dealer-facing response
  (WhatsApp replies, dealer-portal API). Route it through
  `sales.utils.safe_customer_message` first. This was a real production
  leak, not a hypothetical.
- Don't hardcode a business rule that plausibly differs by client — this
  app is white-labeled. Put it in `DMS Sales Settings` (a Single doctype)
  and read it with `frappe.db.get_single_value(...) or DEFAULT`, matching
  the existing fields there.
- If you add or change a doctype's JSON, that's a schema change — it needs
  `bench --site <site> migrate` to actually apply, and existing sites need
  a data-safe path (a patch under `dms_erp/patches/`, listed in
  `patches.txt`) if the change isn't purely additive.
- Don't add comments/docstrings that restate what the code does. Only
  write one when the *why* isn't obvious from the code itself — see
  `CLAUDE.md`'s note on this codebase's docstring register.

## Verifying your change

Per-module `test_*.py` files use `FrappeTestCase` and need a real Frappe
`bench` site to execute. Run them if your environment has one:

```bash
bench --site <site-name> run-tests --app dms_erp
```

At minimum, always run, on every file you touched:

```bash
python3 -m py_compile <changed files>
ruff check <changed files>
```

A clean `py_compile`/`ruff check` is necessary, not sufficient — it catches
syntax and lint issues, not logic errors. Reason through test coverage by
hand if you can't run the suite; don't claim something works without either
running it or explaining why you're confident it does.

If your change affects user-visible behavior, update
`docs/qa-usecase-testcase-guide.md` (add/adjust the relevant test cases) in
the same change — not as a follow-up.

## Commit messages

Explain *why*, not just what changed — the diff already shows what changed.
Keep one logical change per commit rather than bundling an unrelated fix
into the same commit as a feature.

## Pull requests

- Summary of the change and the reasoning behind it, not just a restated
  diff.
- A test plan: what you ran (`py_compile`/`ruff`, `bench run-tests` if
  available) and, if relevant, what you verified by hand.
- If the change resolves a documented "known gap" (in a module README or
  `docs/qa-usecase-testcase-guide.md`'s "Known gaps" section), update that
  doc in the same PR so it stops being listed as a gap.
- If the change is a deliberate deviation from BRD wording, or leaves a BRD
  requirement intentionally unbuilt, say so explicitly and why — don't
  leave it to be discovered later by someone re-reading the BRD.
