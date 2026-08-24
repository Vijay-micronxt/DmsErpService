"""Shared test fixtures for the warehouse module's test_*.py files (not itself a
test module — no test_ prefix)."""

import frappe

from dms_erp.warehouse.bay_api import create_bay
from dms_erp.warehouse.setup import setup_warehouse


def ensure_company() -> str:
	company = frappe.defaults.get_global_default("company")
	if company:
		return company
	company = frappe.get_all("Company", limit=1, pluck="name")
	if company:
		frappe.db.set_default("company", company[0])
		return company[0]
	doc = frappe.get_doc({"doctype": "Company", "company_name": "Pacific Test Co", "default_currency": "INR", "abbr": "PTC"})
	doc.insert(ignore_permissions=True)
	frappe.db.set_default("company", doc.name)
	return doc.name


def make_item(item_code: str, item_group: str = "Vitrified", has_batch_no: int = 1) -> str:
	if frappe.db.exists("Item", item_code):
		frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": item_group,
			"stock_uom": "Box",
			"is_stock_item": 1,
			"has_batch_no": has_batch_no,
			"create_new_batch": 0,
		}
	).insert(ignore_permissions=True)
	return item_code


def make_supplier(supplier_name: str) -> str:
	if frappe.db.exists("Supplier", supplier_name):
		return supplier_name
	frappe.get_doc({"doctype": "Supplier", "supplier_name": supplier_name, "supplier_group": "All Supplier Groups"}).insert(ignore_permissions=True)
	return supplier_name


def make_bay(code: str, bay_type: str = "main", dimensions: str = "36x8", zone: str = "Z", row: str = "R1", categories=None, parent_warehouse: str | None = None):
	ensure_company()
	setup_warehouse()
	if frappe.db.exists("Warehouse", {"custom_bay_code": code}):
		frappe.delete_doc("Warehouse", frappe.db.get_value("Warehouse", {"custom_bay_code": code}, "name"), force=True, ignore_permissions=True)

	parent = parent_warehouse or frappe.db.get_value("Warehouse", {"warehouse_name": "Pacific Main — Morbi"}, "name")
	return create_bay(
		code=code,
		bay_type=bay_type,
		dimensions=dimensions,
		parent_warehouse=parent,
		zone=zone,
		row=row,
		suitable_categories=categories,
	)
