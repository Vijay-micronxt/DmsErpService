"""Order Channel and Dealer Type masters (BRD C.4.3) for the staff app's Order Channels page.

Both are real doctypes, so the same records can be added or changed in ERPNext Desk; this module
is only the JSON API the staff app uses. Reading is open to every staff role (the channel picker
on a quotation or order needs the list); changing them is Management only, because what a channel
counts as decides which orders feed reorder planning and the demand forecast.

Channels are disabled rather than deleted: documents already using one keep it, ERPNext refuses
to delete one that is in use, and the default channel can never be removed (see
OrderChannel.validate)."""

import frappe
from frappe import _
from frappe.utils import cint

MANAGE_ROLES = {"DMS Management", "System Manager"}


def _assert_can_manage():
	if not set(frappe.get_roles(frappe.session.user)) & MANAGE_ROLES:
		frappe.throw(_("Only Management can change order channels and dealer types."), frappe.PermissionError)


def _counts(doctype: str, column: str, extra: str = "") -> dict[str, int]:
	rows = frappe.db.sql(
		f"select {column} as k, count(*) as n from `tab{doctype}` where ifnull({column}, '') != '' {extra} group by {column}",
		as_dict=True,
	)
	return {r.k: r.n for r in rows}


def _serialize_channel(doc, orders: int = 0) -> dict:
	return {
		"id": doc.name,
		"name": doc.channel_name,
		"enabled": bool(doc.enabled),
		"countsAsRetailDemand": bool(doc.counts_as_retail_demand),
		"isDefault": bool(doc.is_default_channel),
		"isBulkQuantity": bool(doc.is_bulk_quantity_channel),
		"orders": orders,
	}


def _serialize_type(doc, dealers: int = 0) -> dict:
	return {
		"id": doc.name,
		"name": doc.type_name,
		"enabled": bool(doc.enabled),
		"forcesChannel": doc.forces_channel,
		"dealers": dealers,
	}


@frappe.whitelist(methods=["GET"])
def list_order_channels(enabled_only: bool = False):
	filters = {"enabled": 1} if cint(enabled_only) else {}
	names = frappe.get_all("Order Channel", filters=filters, pluck="name", order_by="creation asc")
	orders = _counts("Sales Order", "custom_order_channel", "and docstatus != 2")
	return [_serialize_channel(frappe.get_doc("Order Channel", n), orders.get(n, 0)) for n in names]


@frappe.whitelist(methods=["POST"])
def create_order_channel(name: str, counts_as_retail_demand: bool = False):
	"""A new channel is a manual choice on a quotation or order. Automatic classification still
	picks only the default and the bulk-quantity channels, plus whatever a dealer type pins."""
	_assert_can_manage()
	name = (name or "").strip()
	if not name:
		frappe.throw(_("A channel name is required."), frappe.ValidationError)
	if frappe.db.exists("Order Channel", name):
		frappe.throw(_("Channel {0} already exists.").format(name), frappe.DuplicateEntryError)
	doc = frappe.get_doc(
		{
			"doctype": "Order Channel",
			"channel_name": name,
			"enabled": 1,
			"counts_as_retail_demand": 1 if cint(counts_as_retail_demand) else 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return _serialize_channel(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_order_channel(channel: str, patch: dict):
	"""Patch keys: enabled, countsAsRetailDemand, isDefault, isBulkQuantity. Making another
	channel the default (or the bulk-quantity one) moves the flag off the previous holder."""
	_assert_can_manage()
	doc = frappe.get_doc("Order Channel", channel)
	field_map = {
		"enabled": "enabled",
		"countsAsRetailDemand": "counts_as_retail_demand",
		"isDefault": "is_default_channel",
		"isBulkQuantity": "is_bulk_quantity_channel",
	}
	for key, fieldname in field_map.items():
		if key in patch:
			doc.set(fieldname, 1 if patch[key] else 0)
	doc.save(ignore_permissions=True)
	return _serialize_channel(doc)


@frappe.whitelist(methods=["GET"])
def list_dealer_types(enabled_only: bool = False):
	filters = {"enabled": 1} if cint(enabled_only) else {}
	names = frappe.get_all("Dealer Type", filters=filters, pluck="name", order_by="creation asc")
	dealers = _counts("Customer", "custom_dealer_type", "and disabled = 0")
	return [_serialize_type(frappe.get_doc("Dealer Type", n), dealers.get(n, 0)) for n in names]


@frappe.whitelist(methods=["POST"])
def create_dealer_type(name: str, forces_channel: str | None = None):
	_assert_can_manage()
	name = (name or "").strip()
	if not name:
		frappe.throw(_("A dealer type name is required."), frappe.ValidationError)
	if frappe.db.exists("Dealer Type", name):
		frappe.throw(_("Dealer type {0} already exists.").format(name), frappe.DuplicateEntryError)
	doc = frappe.get_doc(
		{"doctype": "Dealer Type", "type_name": name, "enabled": 1, "forces_channel": forces_channel or None}
	)
	doc.insert(ignore_permissions=True)
	return _serialize_type(doc)


@frappe.whitelist(methods=["POST", "PUT"])
def update_dealer_type(dealer_type: str, patch: dict):
	"""Patch keys: enabled, forcesChannel (null clears it)."""
	_assert_can_manage()
	doc = frappe.get_doc("Dealer Type", dealer_type)
	if "enabled" in patch:
		doc.enabled = 1 if patch["enabled"] else 0
	if "forcesChannel" in patch:
		doc.forces_channel = patch["forcesChannel"] or None
	doc.save(ignore_permissions=True)
	return _serialize_type(doc)
