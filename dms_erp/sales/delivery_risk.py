"""BRD C.3.2 -- "Supplier vs Customer Timelines." Two timelines compared: a
Purchase Order line's own `schedule_date` (the supplier/factory commitment) against
an open Sales Order line's `delivery_date` (the customer commitment). Only a line
whose covering supply includes a delayed PO, *and* whose own delivery date is
already imminent, is flagged -- not every delayed PO, and not every open order.

No field anywhere in this app links a specific Purchase Order line to the Sales
Order line(s) it's meant to cover (the one exception, `Purchase Order.
custom_source_inquiry`, is a single header-level Inquiry link set only by
sales.inquiry_api.convert_to_purchase_requirement -- never a Sales Order, never
per-line). `Bin` stock itself is fully fungible (item_code + warehouse only, no
order tag), and nothing in the real fulfillment chain (Purchase Order -> Inward
Truck -> Bay Allocation/Purchase Receipt -> Bin -> picking) reserves incoming
stock to a specific outgoing order -- picking reads live aggregate stock, same as
purchase.po_api.remaining_ready_qty_for_line's own for_update row-lock instead of
a cached reservation count.

So "which orders are at risk" is answered live here, every run, by a deterministic
FIFO match -- oldest open order first (by transaction_date, not delivery_date: a
later order shouldn't jump the queue just because it was promised sooner than one
placed earlier) -- against available supply in priority order: stock on hand,
then on-track in-transit, then on-track still-at-supplier, then delayed
in-transit, then delayed still-at-supplier. Only the last two pools can make a
line at-risk. This is a model of which order is jeopardized, not a recorded
allocation -- nothing in the real process actually assigns PO-2 to order-7 ahead
of order-9 -- the same honesty forecasting.py's own demand_forecast is explicit
about for its own projection.

Every open Sales Order line gets a row (this is also the forecasting/stock
window's own row set, see reports.delivery_risk), but `atRisk` is only ever True
for an item that actually has at least one delayed PO line -- a line left
uncovered on an item with no delay anywhere in its supply is a plain reorder gap
(purchase.reorder_api's own territory), not a C.3.2 case, however imminent its
delivery_date is.
"""

import frappe
from frappe.utils import date_diff, getdate, today

from dms_erp.purchase.reorder_api import _users_with_any_role
from dms_erp.sales.reservation import CLOSED_STAGES, _salesperson

REVIEW_ROLES = ["DMS Management"]

# Worst-to-best doesn't matter here; this is the order pools are *built* in
# (best-to-worst), which is also the order they're *consumed* in -- the FIFO walk
# below relies on that.
_POOL_PRIORITY = ("in_transit_ontrack", "factory_dispatch_ontrack", "in_transit_delayed", "factory_dispatch_delayed")


def _delay_grace_days() -> int:
	return frappe.db.get_single_value("DMS Purchase Settings", "po_delay_grace_days") or 0


def _eta_buffer_days() -> int:
	return frappe.db.get_single_value("DMS Purchase Settings", "delivery_risk_eta_buffer_days") or 0


def _open_po_supply_by_item(as_of, grace_days: int) -> dict[str, list[dict]]:
	"""Every submitted, not-yet-fully-received PO line, split into in-transit (already
	has at least one Inward Truck row booked against it -- physically moving, at the
	gate, unloading or put away, regardless of status) vs factory-dispatch (nothing
	booked yet -- still sitting at the supplier), each tagged delayed/on-track against
	its own schedule_date. One row per PO line; `pending_qty` excludes what's already
	received, same as po_api.list_pending_po_lines."""
	rows = frappe.db.sql(
		"""
		select poi.name as line, poi.parent as po, po.supplier as supplier,
			po.schedule_date as schedule_date, poi.item_code as item,
			(poi.qty - coalesce(poi.received_qty, 0)) as pending_qty,
			coalesce((select sum(it.boxes) from `tabInward Truck` it
					  where it.purchase_order_item = poi.name), 0) as booked_qty
		from `tabPurchase Order Item` poi
		inner join `tabPurchase Order` po on po.name = poi.parent
		where po.docstatus = 1 and po.status not in ('Completed', 'Closed')
		""",
		as_dict=True,
	)
	by_item: dict[str, list[dict]] = {}
	for row in rows:
		pending = row.pending_qty
		if pending <= 0:
			continue
		in_transit_qty = min(row.booked_qty, pending)
		factory_dispatch_qty = pending - in_transit_qty
		days_overdue = date_diff(as_of, row.schedule_date) if row.schedule_date else 0
		delayed = bool(row.schedule_date) and days_overdue > grace_days
		by_item.setdefault(row.item, []).append(
			{
				"po": row.po,
				"line": row.line,
				"supplier": row.supplier,
				"scheduleDate": row.schedule_date,
				"inTransitQty": in_transit_qty,
				"factoryDispatchQty": factory_dispatch_qty,
				"delayed": delayed,
				"daysOverdue": max(days_overdue, 0),
			}
		)
	return by_item


def _open_sales_order_lines_by_item() -> dict[str, list[dict]]:
	"""Every open Sales Order line -- still holding stock per reservation.CLOSED_STAGES,
	the same "fulfilled" definition that module already established -- grouped by item,
	oldest order first (FIFO by transaction_date, then name as a same-day tie-break)."""
	rows = frappe.db.sql(
		"""
		select soi.name as line, soi.parent as sales_order, so.customer as customer,
			soi.item_code as item, soi.qty as qty,
			coalesce(soi.delivery_date, so.delivery_date) as delivery_date,
			soi.custom_delivery_risk_flagged_on as flagged_on
		from `tabSales Order Item` soi
		inner join `tabSales Order` so on so.name = soi.parent
		where so.docstatus = 1 and so.custom_fulfillment_stage not in %(closed)s
		order by so.transaction_date asc, so.name asc
		""",
		{"closed": tuple(CLOSED_STAGES)},
		as_dict=True,
	)
	by_item: dict[str, list[dict]] = {}
	for row in rows:
		by_item.setdefault(row.item, []).append(dict(row))
	return by_item


def _bulk_stock_for_items(item_codes: list[str]) -> dict[str, float]:
	"""Same Bin.actual_qty sum purchase.reorder_api._bulk_stock_for_items uses as the
	app's one ground truth for stock on hand, batched the same way for the same
	N+1-avoidance reason."""
	if not item_codes:
		return {}
	rows = frappe.db.sql(
		"select item_code, sum(actual_qty) as qty from `tabBin` where item_code in %(items)s group by item_code",
		{"items": item_codes},
		as_dict=True,
	)
	return {r.item_code: float(r.qty or 0) for r in rows}


def _build_pools(stock_qty: float, po_lines: list[dict]) -> list[dict]:
	"""One ordered queue per item: stock first, then each PO line's in-transit/
	factory-dispatch qty, on-track lines before delayed ones within each kind. Only
	pools tagged delayed=True can make a demand line at-risk."""
	pools = []
	if stock_qty > 0:
		pools.append({"qty": stock_qty, "source": "stock", "delayed": False, "po": None})
	by_kind = {"in_transit_ontrack": [], "factory_dispatch_ontrack": [], "in_transit_delayed": [], "factory_dispatch_delayed": []}
	for po_line in po_lines:
		suffix = "_delayed" if po_line["delayed"] else "_ontrack"
		if po_line["inTransitQty"] > 0:
			by_kind["in_transit" + suffix].append(po_line)
		if po_line["factoryDispatchQty"] > 0:
			by_kind["factory_dispatch" + suffix].append(po_line)
	for kind in _POOL_PRIORITY:
		field = "inTransitQty" if kind.startswith("in_transit") else "factoryDispatchQty"
		for po_line in by_kind[kind]:
			pools.append({"qty": po_line[field], "source": kind, "delayed": po_line["delayed"], "po": po_line})
	return pools


def at_risk_sales_order_lines(as_of=None) -> list[dict]:
	"""Every open Sales Order line, FIFO-matched against open supply for its item --
	this is also the forecasting/stock window's own row set (reports.delivery_risk
	filters/groups this same output), so every item with open demand gets a row, not
	just items with a delay. `atRisk` is still gated on the item actually having at
	least one delayed PO line, though -- a line left uncovered where no delay is
	involved at all is a plain reorder gap (purchase.reorder_api's own territory, see
	the module docstring), not a C.3.2 case, however imminent its delivery_date is."""
	as_of = getdate(as_of) if as_of else getdate(today())
	grace_days = _delay_grace_days()
	eta_buffer_days = _eta_buffer_days()

	demand_by_item = _open_sales_order_lines_by_item()
	if not demand_by_item:
		return []
	supply_by_item = _open_po_supply_by_item(as_of, grace_days)
	stock_by_item = _bulk_stock_for_items(list(demand_by_item))

	out = []
	for item, demand_lines in demand_by_item.items():
		item_has_delay = any(line["delayed"] for line in supply_by_item.get(item, []))
		pools = _build_pools(stock_by_item.get(item, 0.0), supply_by_item.get(item, []))
		pool_idx = 0
		remaining = pools[0]["qty"] if pools else 0.0

		for demand in demand_lines:
			need = demand["qty"]
			covered_from = None
			covering_po_lines: list[dict] = []
			while need > 0 and pool_idx < len(pools):
				if remaining <= 0:
					pool_idx += 1
					remaining = pools[pool_idx]["qty"] if pool_idx < len(pools) else 0.0
					continue
				take = min(need, remaining)
				need -= take
				remaining -= take
				pool = pools[pool_idx]
				covered_from = pool["source"]  # last pool touched = the worst one reached, by construction
				if pool["po"] and pool["po"] not in covering_po_lines:
					covering_po_lines.append(pool["po"])

			uncovered = need > 0
			covered_from_delayed = covered_from in ("in_transit_delayed", "factory_dispatch_delayed")
			delay_days = max((po["daysOverdue"] for po in covering_po_lines), default=0)

			days_to_delivery = date_diff(demand["delivery_date"], as_of) if demand["delivery_date"] else None
			imminent = days_to_delivery is not None and days_to_delivery <= eta_buffer_days
			# Gated on item_has_delay: an uncovered/delay-sourced line on an item with
			# no delayed supply at all is a plain reorder gap, not a C.3.2 case (see
			# this function's own docstring).
			at_risk = item_has_delay and (covered_from_delayed or uncovered) and imminent

			out.append(
				{
					"salesOrder": demand["sales_order"],
					"salesOrderItem": demand["line"],
					"customer": demand["customer"],
					"item": item,
					"deliveryDate": demand["delivery_date"],
					"coveredFrom": "uncovered" if uncovered else (covered_from or "stock"),
					"atRisk": at_risk,
					"alreadyFlagged": bool(demand["flagged_on"]),
					"delayDays": delay_days or None,
					"coveringPoLines": [
						{"po": po["po"], "supplier": po["supplier"], "scheduleDate": po["scheduleDate"], "daysOverdue": po["daysOverdue"]}
						for po in covering_po_lines
					],
				}
			)
	return out


def _notify_delivery_risk(row: dict) -> int:
	"""Same repeated inline Notification Log insert every daily job in this app
	already uses (reorder_api, po_api, dealer_activity, reservation) -- in-app bell
	only, this app has no outbound staff email/WhatsApp to send through.

	"Dealer notification" (BRD's own wording) is deliberately not a real send here:
	the only function anywhere in this app that makes a real outbound call to any
	external channel is comms.whats91.send_otp_template (OTP login only) -- every
	other "outbound to dealer" write (comms.api._send_message, catalog.sample_api's
	reminder) only inserts a WhatsApp Message log row with no confirmed delivery.
	Adding a second log-only pretend-send here for the same unsolved gap wouldn't
	add real capability, so this notifies the dealer's own salesperson instead --
	the person actually positioned to call them -- same split review_reservations
	already settled on for the structurally identical BRD C.3.1 reminder/flag case."""
	recipients = set(_users_with_any_role(REVIEW_ROLES))
	salesperson = _salesperson(row["customer"])
	if salesperson:
		recipients.add(salesperson)
	subject = (
		f"{row['salesOrder']} for {row['customer']} ({row['item']}) is at risk -- "
		f"a delayed supplier PO threatens the {row['deliveryDate']} delivery date."
	)
	for user in recipients:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"document_type": "Sales Order",
				"document_name": row["salesOrder"],
				"subject": subject,
			}
		).insert(ignore_permissions=True)
	return len(recipients)


def review_delivery_risk(as_of=None) -> dict:
	"""Daily. Flags each newly at-risk Sales Order line once (never re-notifies the
	same line) and notifies its salesperson + Management."""
	as_of = getdate(as_of) if as_of else getdate(today())
	flagged, notified = [], 0

	for row in at_risk_sales_order_lines(as_of=as_of):
		if not row["atRisk"] or row["alreadyFlagged"]:
			continue
		frappe.db.set_value(
			"Sales Order Item", row["salesOrderItem"], "custom_delivery_risk_flagged_on", as_of, update_modified=False
		)
		flagged.append(row["salesOrderItem"])
		notified += _notify_delivery_risk(row)

	return {"flagged": flagged, "notified": notified}
