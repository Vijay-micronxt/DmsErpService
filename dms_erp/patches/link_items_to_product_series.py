"""Registers catalog.series_migration.run() as a real Frappe patch, 9 Nov
2026 -- see that module's own docstring for the full reasoning. This was
run manually (bench execute) against the local test site first and the
result reviewed before being wired in here; a live site gets it automatically
on its next `bench migrate`, the same as every other patch in this file.

Idempotent (series_migration.run's own doing, not just the patch system's
once-per-site tracking) -- safe to invoke again later (e.g. via `bench
execute dms_erp.catalog.series_migration.run --kwargs "{'dry_run': False}"`)
if a future Tally import brings in items under new or existing Item Groups,
without needing a second patch.
"""

from dms_erp.catalog.series_migration import run


def execute():
	run(dry_run=False)
