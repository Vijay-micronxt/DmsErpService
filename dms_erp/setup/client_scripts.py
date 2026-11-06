"""Shared by every module's own setup.py that needs to ship a Desk-form Client
Script (native-Desk gap-sheet audit, 6 Nov 2026) -- one place for the upsert
logic so sales/setup.py, catalog/setup.py etc. don't each reinvent it.
"""

import frappe


def sync_client_scripts(scripts: dict[str, str]) -> None:
	"""`scripts` is {doctype: script_body}. Upsert convention: one Client Script
	per (doctype, Form view), owned by dms_erp -- found by that pair, not by
	name (Client Script autonames by Prompt), so re-running this on every
	migrate updates the same record instead of piling up duplicates. A
	site-admin-authored Client Script for the same doctype would collide with
	this (last write wins) -- none exist on Pacific's site today (checked
	directly), and dms_erp is the only app expected to manage these doctypes'
	Desk-form behaviour."""
	for doctype, script in scripts.items():
		name = frappe.db.get_value("Client Script", {"dt": doctype, "view": "Form"})
		if name:
			doc = frappe.get_doc("Client Script", name)
			doc.script = script
			doc.enabled = 1
			doc.save(ignore_permissions=True)
		else:
			# Client Script autonames by Prompt -- insert() throws "Please set the
			# document name" without an explicit one.
			frappe.get_doc(
				{
					"doctype": "Client Script",
					"name": f"DMS - {doctype}",
					"dt": doctype,
					"view": "Form",
					"script": script,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
