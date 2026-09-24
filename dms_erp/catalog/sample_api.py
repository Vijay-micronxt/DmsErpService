"""Sample & Display Management (BRD C.10) — the module withdrawal_api.py's own
docstring already flagged as "still unbuilt" and stood in proxies for.

Lifecycle: Sales/Management raise a Sample Request for a dealer -> Warehouse/
Management approve it -> issue_sample actually moves a sample out (a real Material
Issue Stock Entry, not the bay-to-bay Material Transfer transfer_api.py uses --
deliberately sidesteps that module's known list_stock_lots bug (0-boxes-found on
genuinely stocked bays) by relying on ERPNext's own native stock validation on
submit instead of re-deriving available qty ourselves) and auto-creates a Display
Placement Slip, which starts the monitoring clock.

issue_sample is also the BRD C.1.5 fix: it calls dealer_catalog_api's unguarded
_set_product_visibility directly, so a dealer's catalog now actually follows their
issued-sample list day to day, per BRD C.1.5, rather than only a separate manual
Purchase/Management toggle.

A daily job (send_display_monitoring_reminders) nudges dealers at 3/6/12 months
(BRD C.10.2) via WhatsApp; real interactive buttons need Meta template approval
this build doesn't have yet (comms/utils.py's verify_webhook_secret covers the same
gap), so the reminder spells out the three reply words in plain text and staff (or
whatever WhatsApp flow-engine middleware eventually exists) log the outcome via
record_reconciliation, which maps the dealer's response to a follow-up action
(Keep / Put-up Replacement / Pullback) per BRD Figure 12.

A Material Issue Stock Entry (unlike Material Transfer) affects the books -- it
debits a stock-adjustment/expense account, which ERPNext resolves from the Company
or Item Group defaults rather than anything this module sets explicitly. If that
account isn't configured on a site yet, issue_sample's submit() will surface
ERPNext's own "missing account" error; this is a standard ERPNext setup
prerequisite, the same as any other Material Issue on that site, not something
dms_erp computes or overrides.

Physical pullback (bringing stock physically back into a bay) is deliberately not
auto-generated here as a Stock Entry — it would need the same batch/lot machinery
transfer_api.py's own bug lives in, and inventing a second, parallel path around
that bug is worse than just leaving the physical re-entry to the existing inward/
allocation flow once a warehouse team member actually has the boxes in hand.
pullback_display only records the condition and resell/write-off/clearance
decision (BRD C.10.4); it does not move stock.
"""

import frappe
from frappe import _
from frappe.utils import add_months, getdate, now_datetime, today

from dms_erp.catalog.dealer_catalog_api import _set_product_visibility
from dms_erp.pagination import clamp
from dms_erp.warehouse.utils import default_company, get_bay

SAMPLE_REQUEST_ROLES = {"DMS Sales", "DMS Management", "System Manager"}
SAMPLE_APPROVE_ROLES = {"DMS Warehouse", "DMS Management", "System Manager"}
SAMPLE_DISPLAY_ROLES = {"DMS Sales", "DMS Warehouse", "DMS Management", "System Manager"}

# BRD C.10.2's "configurable 3/6/12 month" reminder cadence -- like the withdrawal
# thresholds on Series, there's no single doctype-level place to configure this yet,
# so it's a constant here rather than a per-item/series setting.
REMINDER_INTERVALS_MONTHS = (3, 6, 12)

# BRD Figure 12: a dealer's reconciliation reply drives the follow-up action.
# "Still Displayed" alone doesn't distinguish "selling well" from "slow-moving" --
# staff can override `action` explicitly on record_reconciliation when they know
# which one it is; absent that, Still Displayed defaults to the optimistic case.
RESPONSE_DEFAULT_ACTION = {
	"Still Displayed": "Keep",
	"Removed": "Pullback",
	"Loose Not Shown": "Pullback",
}


def _assert_can_request_samples():
	if not set(frappe.get_roles(frappe.session.user)) & SAMPLE_REQUEST_ROLES:
		frappe.throw(_("Only Sales or Management can request samples."), frappe.PermissionError)


def _assert_can_approve_samples():
	if not set(frappe.get_roles(frappe.session.user)) & SAMPLE_APPROVE_ROLES:
		frappe.throw(_("Only Warehouse or Management can approve or issue samples."), frappe.PermissionError)


def _assert_can_manage_display():
	if not set(frappe.get_roles(frappe.session.user)) & SAMPLE_DISPLAY_ROLES:
		frappe.throw(_("Only Sales, Warehouse or Management can manage sample displays."), frappe.PermissionError)


def _serialize_request(doc) -> dict:
	return {
		"id": doc.name,
		"item": doc.item,
		"dealer": doc.dealer,
		"qty": doc.qty,
		"requestedBy": doc.requested_by,
		"requestDate": doc.request_date,
		"approvalStatus": doc.approval_status,
		"approver": doc.approver,
		"batch": doc.batch,
		"sampleQrCode": doc.sample_qr_code,
	}


def _serialize_placement(doc) -> dict:
	return {
		"id": doc.name,
		"sampleRequest": doc.sample_request,
		"item": doc.item,
		"dealer": doc.dealer,
		"displayQty": doc.display_qty,
		"placementDate": doc.placement_date,
		"status": doc.status,
		"condition": doc.condition,
		"photo": doc.photo,
		"pullbackDecision": doc.pullback_decision,
		"lastReminderSent": doc.last_reminder_sent,
		"lastReminderIntervalMonths": doc.last_reminder_interval_months,
	}


def _serialize_reconciliation(doc) -> dict:
	return {
		"id": doc.name,
		"placementSlip": doc.placement_slip,
		"dealer": doc.dealer,
		"item": doc.item,
		"reconDate": doc.recon_date,
		"response": doc.response,
		"respondedVia": doc.responded_via,
		"respondedBy": doc.responded_by,
		"action": doc.action,
		"remarks": doc.remarks,
	}


@frappe.whitelist(methods=["POST"])
def create_sample_request(item: str, dealer: str, qty: float = 1):
	_assert_can_request_samples()

	if float(qty) <= 0:
		frappe.throw(_("Enter a quantity greater than zero."), frappe.ValidationError)

	doc = frappe.get_doc(
		{
			"doctype": "Sample Request",
			"item": item,
			"dealer": dealer,
			"qty": qty,
			"requested_by": frappe.session.user,
			"request_date": today(),
			"approval_status": "Pending",
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize_request(doc)


@frappe.whitelist(methods=["GET"])
def list_sample_requests(status: str | None = None, dealer: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = {}
	if status:
		filters["approval_status"] = status
	if dealer:
		filters["dealer"] = dealer

	total = frappe.db.count("Sample Request", filters=filters)
	names = frappe.get_all(
		"Sample Request", filters=filters, pluck="name", order_by="request_date desc", limit_start=offset, limit_page_length=limit
	)
	return {
		"items": [_serialize_request(frappe.get_doc("Sample Request", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_sample_request(request: str):
	return _serialize_request(frappe.get_doc("Sample Request", request))


@frappe.whitelist(methods=["POST"])
def approve_sample_request(request: str, approve: bool, batch: str | None = None):
	_assert_can_approve_samples()

	doc = frappe.get_doc("Sample Request", request)
	if doc.approval_status != "Pending":
		frappe.throw(_("Only a Pending request can be approved or rejected."), frappe.ValidationError)

	doc.approval_status = "Approved" if approve else "Rejected"
	doc.approver = frappe.session.user
	if batch:
		doc.batch = batch
	doc.save(ignore_permissions=True)
	return _serialize_request(doc)


@frappe.whitelist(methods=["POST"])
def issue_sample(request: str, bay: str, batch_no: str | None = None, photo: str | None = None):
	"""Moves the sample out (BRD C.10.1: "only then moved out, through a proper stock
	movement"), generates its dealer-specific QR, auto-creates the Display Placement
	Slip that starts the monitoring clock, and grants this dealer catalog visibility
	for the item (BRD C.1.5) -- see this module's and dealer_catalog_api's docstrings."""
	_assert_can_approve_samples()

	doc = frappe.get_doc("Sample Request", request)
	if doc.approval_status != "Approved":
		frappe.throw(_("Only an Approved request can be issued."), frappe.ValidationError)

	bay_doc = get_bay(bay)
	entry = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"purpose": "Material Issue",
			"stock_entry_type": "Material Issue",
			"company": default_company(),
			"custom_remarks": f"Sample issued to {doc.dealer} ({doc.name})",
			"items": [
				{
					"item_code": doc.item,
					"qty": doc.qty,
					"batch_no": batch_no or doc.batch,
					"s_warehouse": bay_doc.name,
					# A sample is a goodwill/marketing cost, not a costed sale -- an item with
					# no resolvable valuation rate (never went through a costed Purchase
					# Receipt) would otherwise hard-block ERPNext's own accounting-entry check
					# on submit. This only tolerates a *missing* rate; it doesn't override a
					# real one FIFO/moving-average can already resolve.
					"allow_zero_valuation_rate": 1,
				}
			],
		}
	)
	entry.insert(ignore_permissions=True)
	entry.submit()

	qr_code = f"{doc.item}-{frappe.generate_hash(length=6).upper()}"
	doc.sample_qr_code = qr_code
	doc.approval_status = "Issued"
	doc.save(ignore_permissions=True)

	_grant_dealer_item_sample(doc.dealer, doc.item)

	placement = frappe.get_doc(
		{
			"doctype": "Display Placement Slip",
			"sample_request": doc.name,
			"item": doc.item,
			"dealer": doc.dealer,
			"display_qty": doc.qty,
			"placement_date": today(),
			"status": "Active",
			"photo": photo,
		}
	)
	placement.insert(ignore_permissions=True)

	_set_product_visibility(doc.dealer, doc.item, True)

	return {"sampleRequest": _serialize_request(doc), "placement": _serialize_placement(placement)}


def _grant_dealer_item_sample(dealer: str, item: str):
	"""Sets sample_issued on this dealer's Item Dealer Code row, creating one if this
	dealer has never had a row for this item. customer_item_code is reqd on that
	child doctype, and issuing a sample shouldn't be blocked on the dealer's own code
	being known yet -- it defaults to the company item code as a placeholder;
	update_product's dealerCodes patch is where a real one gets set later."""
	item_doc = frappe.get_doc("Item", item)
	row = next((r for r in item_doc.custom_dealer_codes if r.dealer == dealer), None)
	if row:
		row.sample_issued = 1
	else:
		item_doc.append("custom_dealer_codes", {"dealer": dealer, "customer_item_code": item, "sample_issued": 1})
	item_doc.save(ignore_permissions=True)


@frappe.whitelist(methods=["GET"])
def list_display_placements(dealer: str | None = None, item: str | None = None, status: str | None = None, limit: int = 20, offset: int = 0):
	limit, offset = clamp(limit, offset)
	filters = {}
	if dealer:
		filters["dealer"] = dealer
	if item:
		filters["item"] = item
	if status:
		filters["status"] = status

	total = frappe.db.count("Display Placement Slip", filters=filters)
	names = frappe.get_all(
		"Display Placement Slip",
		filters=filters,
		pluck="name",
		order_by="placement_date desc",
		limit_start=offset,
		limit_page_length=limit,
	)
	return {
		"items": [_serialize_placement(frappe.get_doc("Display Placement Slip", name)) for name in names],
		"total": total,
		"limit": limit,
		"offset": offset,
	}


@frappe.whitelist(methods=["GET"])
def get_display_placement(slip: str):
	return _serialize_placement(frappe.get_doc("Display Placement Slip", slip))


@frappe.whitelist(methods=["POST"])
def record_reconciliation(
	placement_slip: str,
	response: str,
	responded_via: str = "Staff",
	responded_by: str | None = None,
	action: str | None = None,
	remarks: str | None = None,
):
	_assert_can_manage_display()

	if response not in RESPONSE_DEFAULT_ACTION:
		frappe.throw(_("Invalid response: {0}").format(response), frappe.ValidationError)

	placement = frappe.get_doc("Display Placement Slip", placement_slip)
	resolved_action = action or RESPONSE_DEFAULT_ACTION[response]

	recon = frappe.get_doc(
		{
			"doctype": "Sample Display Reconciliation",
			"placement_slip": placement.name,
			"dealer": placement.dealer,
			"item": placement.item,
			"recon_date": today(),
			"response": response,
			"responded_via": responded_via,
			"responded_by": responded_by or (frappe.session.user if responded_via == "Staff" else None),
			"action": resolved_action,
			"remarks": remarks,
		}
	)
	recon.insert(ignore_permissions=True)

	if resolved_action == "Pullback":
		placement.status = "Removed"
	placement.save(ignore_permissions=True)

	return _serialize_reconciliation(recon)


@frappe.whitelist(methods=["POST"])
def pullback_display(placement_slip: str, condition: str, decision: str):
	"""Records that a display was physically pulled back to the warehouse, its
	condition, and the resell/write-off/clearance decision (BRD C.10.4). Does not
	move stock -- see this module's docstring."""
	_assert_can_manage_display()

	placement = frappe.get_doc("Display Placement Slip", placement_slip)
	placement.status = "Pulled Back"
	placement.condition = condition
	placement.pullback_decision = decision
	placement.save(ignore_permissions=True)
	return _serialize_placement(placement)


def send_display_monitoring_reminders() -> int:
	"""Daily scheduled job (BRD C.10.2). For every Active Display Placement Slip,
	once its age crosses the next configured interval (3/6/12 months) not already
	reminded for, sends a WhatsApp nudge asking the dealer to confirm the display's
	status -- see this module's docstring for why it's plain text, not real buttons.
	Returns the number of reminders sent, so callers/tests don't have to re-query."""
	slips = frappe.get_all(
		"Display Placement Slip",
		filters={"status": "Active"},
		fields=["name", "dealer", "item", "placement_date", "last_reminder_interval_months"],
	)

	sent = 0
	for slip in slips:
		due_interval = None
		for interval in REMINDER_INTERVALS_MONTHS:
			if getdate(today()) >= getdate(add_months(slip.placement_date, interval)) and interval > (
				slip.last_reminder_interval_months or 0
			):
				due_interval = interval

		if due_interval is None:
			continue

		item_name = frappe.db.get_value("Item", slip.item, "item_name") or slip.item
		text = (
			f"Quick check on your {item_name} display (placed {due_interval} month(s) ago) -- "
			"reply STILL DISPLAYED, REMOVED, or LOOSE/NOT SHOWN so we can keep it current."
		)
		frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"dealer": slip.dealer,
				"direction": "Outbound",
				"text": text,
				"status": "Sent",
				"related_type": "Display Placement Slip",
				"related_reference": slip.name,
				"sent_at": now_datetime(),
			}
		).insert(ignore_permissions=True)

		frappe.db.set_value(
			"Display Placement Slip",
			slip.name,
			{"last_reminder_sent": today(), "last_reminder_interval_months": due_interval},
		)
		sent += 1
	return sent
