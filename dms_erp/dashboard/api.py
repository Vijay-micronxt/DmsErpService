"""Role-based operating dashboards — read-only aggregation over every other module.
No doctype lives here; there is nothing to store, only to compute.

Most of pacific-tileflow's dashboard.tsx is, honestly, hardcoded demo numbers (a
literal `value="7"` for "Today's Inquiries", a literal `inr(4820000)` for "Total
Sales"), not real computed KPIs — unlike the warehouse-dashboard.ts functions it
also uses, which were real from the start. Now that every other phase's real data
exists, this module computes the real thing everywhere that data actually supports
it, and stubs (clearly, at 0 or empty) only what would need data this app was never
scoped to produce — principally real accounts-receivable ledger data, which needs an
invoicing phase this 8-phase plan never included (BRD flow stops at "Dispatched").
"""

from datetime import date

import frappe
from frappe.utils import add_days

from dms_erp.finance.claims_api import claim_summary
from dms_erp.pricing.api import get_dealer_price
from dms_erp.purchase import po_api
from dms_erp.purchase.reorder_api import reorder_suggestions
from dms_erp.sales.picking_api import list_pick_tasks
from dms_erp.warehouse.bay_api import list_bays
from dms_erp.warehouse.inward_api import list_trucks
from dms_erp.warehouse.transfer_api import list_transfers
from dms_erp.warehouse.utils import list_stock_lots

SALES_READ_ROLES = {"Pacific Sales", "Pacific Management", "System Manager"}
WAREHOUSE_READ_ROLES = {"Pacific Warehouse", "Pacific Management", "System Manager"}
PURCHASE_READ_ROLES = {"Pacific Purchase", "Pacific Management", "System Manager"}
MANAGEMENT_READ_ROLES = {"Pacific Management", "System Manager"}


def _assert_role(allowed: set, message: str):
	if not set(frappe.get_roles(frappe.session.user)) & allowed:
		frappe.throw(message, frappe.PermissionError)


def _month_bounds():
	today = date.today()
	return today.replace(day=1), today


# ==================== Sales ====================

MISSED_DEMAND_STATUSES = {"Out of Stock", "Pre-order Required"}
ACTIONABLE_INQUIRY_STATUSES = ["Open", "Out of Stock", "Partially Available", "Pre-order Required"]


@frappe.whitelist(methods=["GET"])
def sales_dashboard():
	_assert_role(SALES_READ_ROLES, "Only Sales or Management can view the sales dashboard.")

	month_start, today = _month_bounds()

	todays_inquiries = frappe.db.count("Inquiry", {"date": today})
	# Quotation has no single "pending" status; "Open" (submitted, not yet ordered/
	# lost/expired) is the closest native proxy.
	pending_quotations = frappe.db.count("Quotation", {"status": "Open", "quotation_to": "Customer"})

	orders_this_month = frappe.get_all(
		"Sales Order",
		filters={"docstatus": 1, "transaction_date": [">=", month_start]},
		fields=["count(name) as count", "coalesce(sum(grand_total), 0) as value"],
	)[0]

	missed_demand_value = 0
	for row in frappe.get_all(
		"Inquiry",
		filters={"status": ["in", list(MISSED_DEMAND_STATUSES)], "date": [">=", month_start]},
		fields=["item", "qty"],
	):
		price = get_dealer_price(row.item)
		if price:
			missed_demand_value += row.qty * price

	trend_rows = frappe.db.sql(
		"select date, count(name) as count from `tabInquiry` where date >= %s group by date order by date",
		(add_days(today, -30),),
		as_dict=True,
	)

	actionable = frappe.get_all(
		"Inquiry",
		filters={"status": ["in", ACTIONABLE_INQUIRY_STATUSES]},
		fields=["name as id", "dealer as dealerId", "item as productId", "qty", "status"],
		order_by="creation desc",
		limit=5,
	)

	return {
		"todaysInquiries": todays_inquiries,
		"pendingQuotations": pending_quotations,
		"ordersThisMonth": {"count": orders_this_month.count, "value": orders_this_month.value},
		"missedDemandValue": missed_demand_value,
		"inquiryTrend": [{"day": str(r.date), "inquiries": r.count} for r in trend_rows],
		"actionableInquiries": actionable,
	}


# ==================== Warehouse ====================


def _damage_lots_awaiting_claim() -> int:
	damage_bays = frappe.get_all("Warehouse", filters={"custom_bay_type": "damage"}, pluck="name")
	if not damage_bays:
		return 0

	awaiting = 0
	for lot in list_stock_lots():
		if lot["bayId"] not in damage_bays:
			continue
		# `claimRef` is already traced by list_stock_lots for damage/insurance-claim bays.
		if not lot["claimRef"]:
			awaiting += 1
	return awaiting


def _warehouse_alerts(bays, trucks, transfers, pick_tasks) -> list[dict]:
	alerts = []

	for bay in bays:
		if bay["status"] == "active" and bay["type"] != "blocked" and bay["occupancyPct"] >= 95:
			alerts.append(
				{
					"id": f"full-{bay['id']}",
					"title": f"{bay['code']} is at {bay['occupancyPct']}% capacity",
					"detail": f"{bay['name']} is nearly full — plan an overflow bay.",
					"priority": "High" if bay["occupancyPct"] >= 100 else "Medium",
				}
			)

	for truck in trucks:
		if truck["status"] == "Unloading":
			alerts.append(
				{
					"id": f"dock-{truck['id']}",
					"title": f"{truck['lr']} unloaded, not put away",
					"detail": f"{truck['supplier']} · {truck['boxes']} boxes still staged at the dock.",
					"priority": "High",
				}
			)

	buffer_bay_ids = {b["id"] for b in bays if b["type"] == "buffer"}
	buffer_lots = sum(1 for l in list_stock_lots() if l["bayId"] in buffer_bay_ids)
	if buffer_lots:
		alerts.append(
			{
				"id": "buffer-aging",
				"title": f"{buffer_lots} lot(s) sitting in buffer bays",
				"detail": "Review buffer bays for transfer back to main storage.",
				"priority": "Medium",
			}
		)

	pending_picks = sum(1 for t in pick_tasks if t["status"] == "Pending")
	if pending_picks:
		alerts.append(
			{
				"id": "pending-picks",
				"title": f"{pending_picks} pick task(s) unallocated",
				"detail": "Orders waiting on bay allocation before they can be picked.",
				"priority": "High",
			}
		)

	damage_lots = _damage_lots_awaiting_claim()
	if damage_lots:
		alerts.append(
			{
				"id": "damage-unclaimed",
				"title": f"{damage_lots} damaged lot(s) pending insurance claim",
				"detail": "File a claim for stock already moved to a damage/insurance bay.",
				"priority": "Medium",
			}
		)

	if transfers:
		recent = transfers[0]
		alerts.append(
			{
				"id": f"transfer-{recent['id']}",
				"title": f"{recent['ref']} · {recent['qty']} boxes moved",
				"detail": f"{recent['transferType']} — {recent['reason']}",
				"priority": "Low",
			}
		)

	for bay in bays:
		if bay["status"] == "blocked":
			alerts.append(
				{
					"id": f"blocked-{bay['id']}",
					"title": f"{bay['code']} is blocked",
					"detail": "Release or reassign once available again.",
					"priority": "Low",
				}
			)

	order = {"High": 0, "Medium": 1, "Low": 2}
	alerts.sort(key=lambda a: order[a["priority"]])
	return alerts


@frappe.whitelist(methods=["GET"])
def warehouse_dashboard():
	_assert_role(WAREHOUSE_READ_ROLES, "Only Warehouse or Management can view the warehouse dashboard.")

	bays = list_bays()
	trucks = list_trucks()
	transfers = list_transfers()
	pick_tasks = list_pick_tasks()
	lots = list_stock_lots()

	total_capacity = sum(b["capacityBoxes"] or 0 for b in bays)
	total_used = sum(l["boxes"] for l in lots)
	buffer_bay_ids = {b["id"] for b in bays if b["type"] == "buffer"}

	kpis = {
		"totalBays": len(bays),
		"occupancyRatePct": round(total_used / total_capacity * 100) if total_capacity else 0,
		"itemsInBuffer": sum(1 for l in lots if l["bayId"] in buffer_bay_ids),
		"pendingAllocationsToday": sum(1 for t in trucks if t["status"] != "Put-away"),
		"damageAwaitingClaim": _damage_lots_awaiting_claim(),
	}

	return {
		"kpis": kpis,
		"alerts": _warehouse_alerts(bays, trucks, transfers, pick_tasks)[:6],
		"incomingTrucksToday": [t for t in trucks if t["status"] != "Put-away"],
	}


# ==================== Purchase ====================


@frappe.whitelist(methods=["GET"])
def purchase_dashboard():
	_assert_role(PURCHASE_READ_ROLES, "Only Purchase or Management can view the purchase dashboard.")

	today = date.today()
	pending_lines = po_api.list_pending_po_lines()
	pending_po_names = {l["po"] for l in pending_lines}
	supplier_delay_po_names = {l["po"] for l in pending_lines if l["daysOverdue"] > 0}

	window_end = add_days(today, 7)
	pickup_plans_this_week = frappe.db.count(
		"Inward Truck", {"status": ["!=", "Put-away"], "eta": ["between", [today, window_end]]}
	)

	suggestions = [s for s in reorder_suggestions() if s["suggestedQty"] > 0]

	trend_rows = frappe.db.sql(
		"""
		select date_format(po.transaction_date, '%%Y-%%m') as month,
			coalesce(sum(poi.qty * poi.rate), 0) as value
		from `tabPurchase Order` po
		inner join `tabPurchase Order Item` poi on poi.parent = po.name
		where po.docstatus = 1
		group by month
		order by month desc
		limit 6
		""",
		as_dict=True,
	)

	return {
		"pendingPOs": len(pending_po_names),
		"supplierDelays": len(supplier_delay_po_names),
		"pickupPlansThisWeek": pickup_plans_this_week,
		"reorderSuggestionsCount": len(suggestions),
		"purchaseTrend": list(reversed([{"month": r.month, "value": r.value} for r in trend_rows])),
		"materialsReadyForPickup": po_api.list_materials_ready_for_pickup(),
	}


# ==================== Management ====================


@frappe.whitelist(methods=["GET"])
def management_dashboard():
	_assert_role(MANAGEMENT_READ_ROLES, "Only Management can view the management dashboard.")

	month_start, _today = _month_bounds()

	total_sales_mtd = frappe.db.sql(
		"select coalesce(sum(grand_total), 0) from `tabSales Order` where docstatus=1 and transaction_date >= %s",
		(month_start,),
	)[0][0]

	top_item_row = frappe.db.sql(
		"""
		select soi.item_code as item, sum(soi.qty) as qty
		from `tabSales Order Item` soi
		inner join `tabSales Order` so on so.name = soi.parent
		where so.docstatus = 1
		group by soi.item_code
		order by qty desc
		limit 1
		""",
		as_dict=True,
	)
	top_moving_item = None
	if top_item_row:
		from dms_erp.warehouse.utils import total_stock_for_item

		item = top_item_row[0]
		top_moving_item = {"itemCode": item.item, "unitsSold": item.qty, "currentStock": total_stock_for_item(item.item)}

	sales_by_dealer = frappe.db.sql(
		"""
		select customer as dealer, coalesce(sum(grand_total), 0) as value
		from `tabSales Order`
		where docstatus = 1
		group by customer
		order by value desc
		limit 10
		""",
		as_dict=True,
	)

	return {
		"totalSalesMtd": total_sales_mtd,
		# No invoicing/AR-ledger phase exists yet (the BRD flow this app implements
		# stops at "Dispatched") — a real receivables figure would need Sales
		# Invoice + Payment Entry data this app has never posted. Honest 0, not a
		# guess.
		"outstandingReceivables": 0,
		"claimableValue": claim_summary()["receivable"],
		"topMovingItem": top_moving_item,
		"salesByDealer": [{"dealer": r.dealer, "value": r.value} for r in sales_by_dealer],
		"alerts": _credit_exposure_alerts(),
	}


def _credit_exposure_alerts() -> list[dict]:
	"""Order value against Customer.credit_limit — a real, native figure, but an
	approximation of true credit risk: it's every submitted Sales Order's value, not
	actual outstanding receivables net of delivery/payment, since this app posts no
	Sales Invoice. Flag if a real AR-aging phase should replace this."""
	rows = frappe.db.sql(
		"""
		select c.name as dealer, c.customer_name as dealer_name, c.credit_limit as credit_limit,
			coalesce(sum(so.grand_total), 0) as order_value
		from `tabCustomer` c
		inner join `tabSales Order` so on so.customer = c.name and so.docstatus = 1
		where c.credit_limit > 0
		group by c.name
		having order_value > c.credit_limit
		""",
		as_dict=True,
	)
	return [
		{
			"id": f"credit-{r.dealer}",
			"title": "Credit limit exceeded (by order value)",
			"detail": f"{r.dealer_name} — order value {r.order_value:.0f} of {r.credit_limit:.0f} limit",
			"priority": "High",
		}
		for r in rows
	]
