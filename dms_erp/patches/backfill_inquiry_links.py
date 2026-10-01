"""One-time backfill for Inquiry.linked_sales_order (added 2026-09-24, commit
d788b8c) and Inquiry.linked_quotation (added 2026-09-25, commit 3341bfd) --
both are set at conversion/quoting time going forward, but any Inquiry that
had already reached "Quoted"/"Converted to Order" before its field existed
has nowhere this was ever recorded. Matched back via the same references the
live code itself uses (Sales Order.custom_source_ref, Quotation.custom_inquiry),
so this only fills rows the real code would already agree on -- never guesses.

A quotation that merged several inquiries only ever has custom_inquiry pointing
at the first one (same limitation the live code has for new merges) -- the
other merged inquiries' own linked_quotation can't be recovered this way,
since nothing else on the Quotation records them.
"""

import frappe


def execute():
	# Inquiry -> Order directly (order_api._create_order / dealer_portal_api.convert_to_order).
	direct_orders = frappe.db.sql(
		"""
		select i.name as inquiry, so.name as sales_order
		from `tabInquiry` i
		join `tabSales Order` so
			on so.custom_source_type = 'Inquiry' and so.custom_source_ref = i.name
		where i.status = 'Converted to Order' and coalesce(i.linked_sales_order, '') = ''
		""",
		as_dict=True,
	)

	# Inquiry -> Quotation -> Order (quotation_api._convert_to_order), via that
	# Quotation's own custom_inquiry back-reference.
	orders_via_quotation = frappe.db.sql(
		"""
		select i.name as inquiry, so.name as sales_order
		from `tabInquiry` i
		join `tabQuotation` q on q.custom_inquiry = i.name
		join `tabSales Order` so
			on so.custom_source_type = 'Quotation' and so.custom_source_ref = q.name
		where i.status = 'Converted to Order' and coalesce(i.linked_sales_order, '') = ''
		""",
		as_dict=True,
	)

	for row in direct_orders + orders_via_quotation:
		frappe.db.set_value("Inquiry", row.inquiry, "linked_sales_order", row.sales_order, update_modified=False)

	# Inquiry -> Quotation, via that Quotation's own custom_inquiry back-reference --
	# covers both still-"Quoted" inquiries and ones already "Converted to Order" from there.
	quoted = frappe.db.sql(
		"""
		select i.name as inquiry, q.name as quotation
		from `tabInquiry` i
		join `tabQuotation` q on q.custom_inquiry = i.name
		where i.status in ('Quoted', 'Converted to Order') and coalesce(i.linked_quotation, '') = ''
		""",
		as_dict=True,
	)
	for row in quoted:
		frappe.db.set_value("Inquiry", row.inquiry, "linked_quotation", row.quotation, update_modified=False)
