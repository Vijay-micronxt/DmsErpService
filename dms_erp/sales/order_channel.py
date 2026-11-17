"""Order-channel auto-classification (BRD C.4.3). `custom_dealer_type` is a manually-set
Customer category that, together with a Series' bulk_qty_threshold (propagated onto Item by
catalog.api.create_product), drives the default `custom_order_channel` on a new
Quotation/Order.

The channels and dealer types are masters (Order Channel / Dealer Type doctypes), not lists
written into the code, so the business can add one in Desk or on the staff app's Order Channels
page. What the system does with a channel comes from flags on the channel itself: which one is
the default, which one a bulk quantity gets, and whether it counts as retail demand for
reorder planning, the forecast and the sample logic. Nothing outside this module should
compare a channel name to "Retail" or "Bulk".

The BRD's "audit-locked manual override" (also BRD C.11 trigger #6) is: a caller
that passes `channel` explicitly (including "Retail") always wins over
auto-classification -- but *making it stick* runs through gate_channel_override
below, not straight through. An already-authorized user's override still applies
immediately (they're the authority BRD C.11 requires), just logged as an
auto-approved Approval Request for audit; anyone else's override attempt is queued
as a Pending one instead, and the document isn't created until Management decides
it (see approvals.api.decide_approval / APPLIERS["Channel Override"]).
"""

import frappe
from frappe import _

CHANNEL_OVERRIDE_AUTHORIZED_ROLES = {"DMS Management", "System Manager"}

# What a fresh site starts with, and what every site had before channels became a master
# (same names, so existing documents keep a valid value). Seeded once by setup_sales.
SEED_ORDER_CHANNELS = [
	{"channel_name": "Retail", "counts_as_retail_demand": 1, "is_default_channel": 1, "is_bulk_quantity_channel": 0},
	{"channel_name": "Bulk", "counts_as_retail_demand": 0, "is_default_channel": 0, "is_bulk_quantity_channel": 1},
	{"channel_name": "Project", "counts_as_retail_demand": 0, "is_default_channel": 0, "is_bulk_quantity_channel": 0},
]
SEED_DEALER_TYPES = [
	{"type_name": "Retail", "forces_channel": None},
	{"type_name": "Bulk", "forces_channel": "Bulk"},
	{"type_name": "Project", "forces_channel": "Project"},
]
# Last resort if the master is empty or broken: the channel this app has always used by default.
FALLBACK_DEFAULT_CHANNEL = "Retail"


def enabled_channel_names() -> list[str]:
	return frappe.get_all("Order Channel", filters={"enabled": 1}, pluck="name", order_by="name asc")


def is_valid_channel(channel: str | None) -> bool:
	return bool(channel) and bool(frappe.db.get_value("Order Channel", channel, "enabled"))


def assert_valid_channel(channel: str | None):
	if not is_valid_channel(channel):
		frappe.throw(_("Invalid channel: {0}").format(channel), frappe.ValidationError)


def default_channel() -> str:
	return frappe.db.get_value("Order Channel", {"is_default_channel": 1, "enabled": 1}, "name") or FALLBACK_DEFAULT_CHANNEL


def bulk_quantity_channel() -> str | None:
	return frappe.db.get_value("Order Channel", {"is_bulk_quantity_channel": 1, "enabled": 1}, "name")


def retail_demand_channels() -> tuple:
	"""Channels whose orders count as retail demand (reorder planning, the forecast, the
	sample / top-dealer logic). Never empty, so it is safe inside an SQL `in %s`: with nothing
	ticked it names the default channel, which is what those queries always meant."""
	names = frappe.get_all("Order Channel", filters={"counts_as_retail_demand": 1}, pluck="name")
	return tuple(names or [default_channel()])


def is_valid_dealer_type(dealer_type: str | None) -> bool:
	return bool(dealer_type) and bool(frappe.db.get_value("Dealer Type", dealer_type, "enabled"))


def non_retail_dealer_types() -> tuple:
	"""Dealer types whose orders are always pinned to a channel that is NOT retail demand
	(Bulk and Project today). Their dealers are left out of the top-dealer / sample logic.
	Never empty, for the same reason as retail_demand_channels."""
	non_retail = frappe.get_all("Order Channel", filters={"counts_as_retail_demand": 0}, pluck="name")
	if not non_retail:
		return ("__none__",)
	names = frappe.get_all("Dealer Type", filters={"forces_channel": ["in", non_retail]}, pluck="name")
	return tuple(names or ["__none__"])


def auto_classify_channel(dealer: str, lines: list[dict]) -> str:
	"""A dealer type that pins a channel (Bulk, Project) always wins -- a bulk dealer's small
	order is still bulk. Otherwise the bulk-quantity channel if any line's qty meets its item's
	Series-sourced bulk_qty_threshold. An item with no threshold set (0/unset -- no Series, or
	a Series that doesn't define one) never triggers the qty check on its own, so an untagged
	catalog gets the default channel, exactly as it always has."""
	dealer_type = frappe.db.get_value("Customer", dealer, "custom_dealer_type")
	if dealer_type:
		pinned = frappe.db.get_value("Dealer Type", dealer_type, "forces_channel")
		if pinned and is_valid_channel(pinned):
			return pinned

	bulk = bulk_quantity_channel()
	if bulk:
		for line in lines:
			threshold = frappe.db.get_value("Item", line["item"], "custom_bulk_qty_threshold")
			if threshold and line["qty"] >= threshold:
				return bulk

	return default_channel()


def seed_masters():
	"""Create the starting channels and dealer types if they are missing. Idempotent, and it
	never touches one that already exists -- so it can run on every migrate without undoing
	changes made in Desk or on the Order Channels page."""
	for row in SEED_ORDER_CHANNELS:
		if not frappe.db.exists("Order Channel", row["channel_name"]):
			frappe.get_doc({"doctype": "Order Channel", "enabled": 1, **row}).insert(ignore_permissions=True)
	for row in SEED_DEALER_TYPES:
		if not frappe.db.exists("Dealer Type", row["type_name"]):
			frappe.get_doc({"doctype": "Dealer Type", "enabled": 1, **row}).insert(ignore_permissions=True)


def gate_channel_override(
	*, requested_channel: str, default_channel: str, reference_doctype: str, create_fn, create_kwargs: dict
) -> dict:
	"""BRD C.11 trigger #6. `requested_channel` is what the caller resolved to
	(explicit or auto-classified); `default_channel` is always
	auto_classify_channel's own answer for the same dealer/lines, so the two only
	differ when a caller actually overrode it. No difference -> create_fn runs
	unconditionally, no approval-queue overhead for the common case.

	`create_fn(**create_kwargs)` must be the trigger's own "_unchecked" creation
	core (quotation_api._create_quotation / order_api._create_order) so that
	replaying it later from an approved request (approvals.api.APPLIERS) can't
	re-enter this gate and loop.
	"""
	if requested_channel == default_channel:
		return create_fn(**create_kwargs)

	from dms_erp.approvals.api import gate_document_creation

	return gate_document_creation(
		trigger_type="Channel Override",
		reference_doctype=reference_doctype,
		reason=(
			f"{frappe.session.user} set channel to {requested_channel} "
			f"(auto-classified: {default_channel})."
		),
		create_fn=create_fn,
		create_kwargs=create_kwargs,
		authorized_roles=CHANNEL_OVERRIDE_AUTHORIZED_ROLES,
	)
