"""Order Channel (BRD C.4.3) -- the retail / bulk / project tag every quotation, order and
invoice carries, as a list the business can extend instead of three words written into the
code. Each channel says how the system treats it (does it count as retail demand for reorder
planning; is it the default; is it what a bulk quantity gets), so adding one needs no
developer: add it here in Desk, or on the staff app's Order Channels page.

Exactly one channel is the default and at most one is the bulk-quantity channel. Ticking
either on a channel moves it off the previous holder, so the rule can't end up ambiguous; the
default can't be un-ticked or disabled without naming a replacement, since automatic
classification would then have nothing to fall back to."""

import frappe
from frappe import _
from frappe.model.document import Document


class OrderChannel(Document):
	def validate(self):
		self.channel_name = (self.channel_name or "").strip()
		for flag, label in (("is_default_channel", "default"), ("is_bulk_quantity_channel", "bulk-quantity")):
			if self.get(flag) and not self.enabled:
				frappe.throw(_("A disabled channel can't be the {0} channel.").format(label), frappe.ValidationError)
		if self.is_default_channel:
			self._move_flag("is_default_channel")
		elif not self.is_new() and frappe.db.get_value("Order Channel", self.name, "is_default_channel"):
			frappe.throw(
				_("{0} is the default channel. Tick Default channel on another channel instead of clearing it here.").format(self.name),
				frappe.ValidationError,
			)
		if self.is_bulk_quantity_channel:
			self._move_flag("is_bulk_quantity_channel")

	def _move_flag(self, flag: str):
		for other in frappe.get_all("Order Channel", filters={flag: 1, "name": ["!=", self.name]}, pluck="name"):
			frappe.db.set_value("Order Channel", other, flag, 0, update_modified=False)

	def on_trash(self):
		if self.is_default_channel:
			frappe.throw(_("{0} is the default channel and can't be deleted.").format(self.name), frappe.ValidationError)
