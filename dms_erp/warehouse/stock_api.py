"""Stock visibility (BRD §8-9) and the bay-suggestion/validation logic used by Bay
Allocation. Everything here is a read/compute over Warehouse+Bin+Stock Ledger Entry —
no data is owned or stored by this file.
"""

import frappe

from dms_erp.warehouse import utils


@frappe.whitelist(methods=["GET"])
def list_stock(bay: str | None = None, item: str | None = None):
	return utils.list_stock_lots(bay=bay, item=item)


@frappe.whitelist(methods=["GET"])
def suggest_bays(category: str, qty: float, kind: str = "normal"):
	return utils.suggest_bays(category, qty, kind)


@frappe.whitelist(methods=["GET"])
def validate_allocation(bay: str, qty: float, category: str):
	return utils.validate_allocation(bay, qty, category)
