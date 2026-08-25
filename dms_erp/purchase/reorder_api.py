"""Reorder-suggestion engine (BRD §12), scoped to what's actually wired up so far.

The real formula (§12.3) weighs missed retail demand, open retail inquiries, and six
months of retail sales velocity — those all come from Inquiry/Order data that only
exists once Phase 5 lands. Until then `missedDemandQty`, `pendingInquiryQty` and
`recentRetailSalesQty` are honest zeros, not fabricated numbers — matching the same
"stub what we don't have data for yet" approach used for Product.stockQty in Phase 2
before Phase 3 landed. `currentStock` and the safety-stock/non-reorderable logic
below are real today, sourced from Phase 3's live Bin aggregate.
"""

import frappe

from dms_erp.catalog.utils import is_reorderable
from dms_erp.warehouse.utils import total_stock_for_item

SAFETY_STOCK_BOXES = 100

URGENCY_STYLES_ORDER = ["Critical", "High", "Watch", "Healthy"]


@frappe.whitelist(methods=["GET"])
def reorder_suggestions():
	items = frappe.get_all("Item", fields=["name", "custom_discontinuation_status"])
	suggestions = [_suggestion_for(item, total_stock_for_item(item.name)) for item in items]
	rank = {u: i for i, u in enumerate(URGENCY_STYLES_ORDER)}
	suggestions.sort(key=lambda s: rank[s["urgency"]])
	return suggestions


def _suggestion_for(item, current_stock: float) -> dict:
	status = item.custom_discontinuation_status or "Active"
	non_reorderable = not is_reorderable(status)

	# Placeholders pending Phase 5 (Inquiries/Orders) — see module docstring.
	missed_demand_qty = 0
	pending_inquiry_qty = 0
	recent_retail_sales_qty = 0

	reasons = []
	if missed_demand_qty > 0:
		reasons.append(f"{missed_demand_qty} boxes of missed/constrained retail demand")
	if pending_inquiry_qty > 0:
		reasons.append(f"{pending_inquiry_qty} boxes in open retail inquiries")
	if current_stock == 0:
		reasons.append("Zero stock on hand")
	elif current_stock < SAFETY_STOCK_BOXES:
		reasons.append(f"Below {SAFETY_STOCK_BOXES}-box safety stock")

	raw_need = missed_demand_qty + pending_inquiry_qty + SAFETY_STOCK_BOXES - current_stock
	suggested_qty = 0 if non_reorderable else max(0, round(raw_need / 10) * 10)

	urgency = "Healthy"
	if not non_reorderable:
		if current_stock == 0 and missed_demand_qty > 0:
			urgency = "Critical"
		elif suggested_qty > 0 and missed_demand_qty > 0:
			urgency = "High"
		elif suggested_qty > 0:
			urgency = "Watch"
	if non_reorderable and missed_demand_qty > 0:
		reasons.append(f"{status} — demand exists but not reorderable")

	return {
		"productId": item.name,
		"currentStock": current_stock,
		"missedDemandQty": missed_demand_qty,
		"pendingInquiryQty": pending_inquiry_qty,
		"recentRetailSalesQty": recent_retail_sales_qty,
		"suggestedQty": suggested_qty,
		"urgency": urgency,
		"reasons": reasons,
		"nonReorderable": non_reorderable,
	}
