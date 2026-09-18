"""Order-channel auto-classification (BRD C.4.3). `custom_dealer_type` (Retail /
Bulk / Project) is a manually-set Customer category that, together with a Series'
bulk_qty_threshold (propagated onto Item by catalog.api.create_product), drives
the default `custom_order_channel` (Retail/Bulk/Project) on a new Quotation/Order.

The BRD's "audit-locked manual override" is simply: a caller that passes `channel`
explicitly (including "Retail") always wins — auto_classify_channel is only ever
consulted when create_quotation/create_order are called without one.
"""

import frappe

DEALER_TYPES = ["Retail", "Bulk", "Project"]


def auto_classify_channel(dealer: str, lines: list[dict]) -> str:
	"""Bulk/Project dealers always default to their own type. Otherwise, Bulk if any
	line's qty meets its item's Series-sourced bulk_qty_threshold. An item with no
	threshold set (0/unset — no Series, or a Series that doesn't define one) never
	triggers the qty check on its own, so an untagged catalog defaults to Retail
	exactly as it always has."""
	dealer_type = frappe.db.get_value("Customer", dealer, "custom_dealer_type")
	if dealer_type in ("Bulk", "Project"):
		return dealer_type

	for line in lines:
		threshold = frappe.db.get_value("Item", line["item"], "custom_bulk_qty_threshold")
		if threshold and line["qty"] >= threshold:
			return "Bulk"

	return "Retail"
