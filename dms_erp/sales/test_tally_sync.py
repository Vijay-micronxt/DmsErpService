from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.sales import tally_sync as T


class TestTallyQty(FrappeTestCase):
	"""Tally keeps "Box of N peices" items as boxes + loose pieces; the ERP keeps a
	decimal box count. A wrong conversion here is a wrong stock figure in Tally."""

	def test_compound_unit_boxes_and_pieces(self):
		self.assertEqual(T.tally_qty(2.333333, "Box", ("peices", 9)), " 2 Box 3 peices")
		self.assertEqual(T.tally_qty(3.25, "Box", ("peices", 4)), " 3 Box 1 peices")
		self.assertEqual(T.tally_qty(0.111111, "Box", ("peices", 9)), " 0 Box 1 peices")
		self.assertEqual(T.tally_qty(5, "Box", ("peices", 2)), " 5 Box 0 peices")

	def test_rounding_noise_rolls_into_a_whole_box(self):
		# 1.999999 boxes of a 2-piece box is 2 boxes, not "1 Box 2 peices"
		self.assertEqual(T.tally_qty(1.999999, "Box", ("peices", 2)), " 2 Box 0 peices")

	def test_fraction_that_is_not_whole_pieces_is_refused(self):
		with self.assertRaises(T.TallyMappingError):
			T.tally_qty(0.3, "Box", ("peices", 4))

	def test_simple_unit(self):
		self.assertEqual(T.tally_qty(12, "Slabs"), " 12 Slabs")
		self.assertEqual(T.tally_qty(1.5, "Box"), " 1.5 Box")


class TestTallyNames(FrappeTestCase):
	def test_ledger_name_drops_company_abbr(self):
		with patch.object(frappe, "get_cached_value", return_value="PI"):
			self.assertEqual(T.tally_ledger_name("09% Output CGST - PI", "Pacific Inc"), "09% Output CGST")
			self.assertEqual(T.tally_ledger_name("Some Ledger", "Pacific Inc"), "Some Ledger")

	def test_main_leaf_warehouse_is_the_tally_parent_godown(self):
		with patch.object(frappe, "get_cached_value", return_value="Godown No 256/7 Kalkere (Main)"):
			self.assertEqual(T.tally_godown_name("Godown No 256/7 Kalkere (Main) - PI"), "Godown No 256/7 Kalkere")
		with patch.object(frappe, "get_cached_value", return_value="No.256/7 - Bay D6"):
			self.assertEqual(T.tally_godown_name("No.256/7 - Bay D6 - PI"), "No.256/7 - Bay D6")

	def test_shared_batch_label_drops_item_suffix(self):
		with patch.object(frappe, "get_cached_value", return_value=None):
			item = "BCP Matt Athangudi Leaf 300x300 Ceramic Glazed FL Tiles 09 Pcs"
			self.assertEqual(T.tally_batch_name(f"308 - {item}", item), "308")
			self.assertEqual(T.tally_batch_name("260808 D-T", item), "260808 D-T")


def _doc(**kw):
	base = {"name": "SINV-TEST-1", "docstatus": 1, "sync_to_tally": 1, "tally_sync_status": "", "is_return": 0}
	base.update(kw)
	return frappe._dict(base)


class TestQueueRules(FrappeTestCase):
	"""What decides whether an invoice is sent at all -- opt-in, submitted only, never
	back to Tally what came from Tally (the loop guard), never twice."""

	def _queue(self, doc):
		with patch.object(T, "_set") as setter:
			T._queue_if_requested(doc, {"tally_sync_status": doc.get("tally_sync_status")})
		return setter.call_args.kwargs if setter.called else None

	def test_ticked_and_submitted_is_queued(self):
		self.assertEqual(self._queue(_doc())["tally_sync_status"], "Pending")

	def test_not_ticked_or_draft_is_left_alone(self):
		self.assertIsNone(self._queue(_doc(sync_to_tally=0)))
		self.assertIsNone(self._queue(_doc(docstatus=0)))

	def test_pending_or_synced_is_not_queued_again(self):
		self.assertIsNone(self._queue(_doc(tally_sync_status="Pending")))
		self.assertIsNone(self._queue(_doc(tally_sync_status="Synced")))

	def test_failed_is_requeued_on_retick(self):
		self.assertEqual(self._queue(_doc(tally_sync_status="Failed"))["tally_sync_status"], "Pending")

	def test_invoice_that_came_from_tally_is_never_sent_back(self):
		self.assertEqual(self._queue(_doc(tally_voucher_no="PI/04189/26-27"))["tally_sync_status"], "Skipped")
		self.assertEqual(self._queue(_doc(tally_guid="abc-123"))["tally_sync_status"], "Skipped")

	def test_credit_note_is_skipped(self):
		self.assertEqual(self._queue(_doc(is_return=1))["tally_sync_status"], "Skipped")


class _Invoice:
	"""Stand-in for a Sales Invoice document: frappe._dict won't do, its .items is the
	dict method, not the child table."""

	def __init__(self, **kw):
		self.__dict__.update(kw)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


class _Saved:
	"""A submitted invoice being saved/cancelled from a stale copy: its own sync fields
	are old, get_doc_before_save() has what the database really held."""

	def __init__(self, before, **kw):
		self.__dict__.update({"name": "SINV-TEST-1", "docstatus": 1, "sync_to_tally": 1, "is_return": 0, **kw})
		self._before = frappe._dict(before) if before is not None else None

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value

	def get_doc_before_save(self):
		return self._before


class TestStaleCopyDoesNotUndoSync(FrappeTestCase):
	def test_update_from_stale_copy_keeps_synced_and_does_not_requeue(self):
		before = {"tally_sync_status": "Synced", "tally_synced_voucher_no": "PI/04185/26-27", "tally_synced_on": None, "tally_sync_error": None}
		doc = _Saved(before, tally_sync_status="", tally_synced_voucher_no=None)
		with patch.object(T, "_set") as setter:
			T.on_update_after_submit(doc)
		restored = setter.call_args_list[0].kwargs
		self.assertEqual(restored["tally_sync_status"], "Synced")
		self.assertEqual(restored["tally_synced_voucher_no"], "PI/04185/26-27")
		self.assertEqual(len(setter.call_args_list), 1)  # restored only -- not queued again

	def test_cancel_right_after_submit_marks_skipped(self):
		before = {"tally_sync_status": "Pending", "tally_synced_voucher_no": None, "tally_synced_on": None, "tally_sync_error": None}
		doc = _Saved(before, docstatus=2, tally_sync_status="")
		with patch.object(T, "_set") as setter:
			T.on_cancel(doc)
		self.assertEqual(setter.call_args_list[-1].kwargs["tally_sync_status"], "Skipped")

	def test_cancel_after_sync_keeps_voucher_and_adds_note(self):
		before = {"tally_sync_status": "Synced", "tally_synced_voucher_no": "PI/04185/26-27", "tally_synced_on": None, "tally_sync_error": None}
		doc = _Saved(before, docstatus=2, tally_sync_status="", tally_synced_voucher_no=None)
		with patch.object(T, "_set") as setter:
			T.on_cancel(doc)
		last = setter.call_args_list[-1].kwargs
		self.assertEqual(last["tally_sync_status"], "Synced")
		self.assertEqual(last["tally_synced_voucher_no"], "PI/04185/26-27")
		self.assertIn("PI/04185/26-27", last["tally_sync_error"])


class TestVoucherXml(FrappeTestCase):
	"""The voucher must balance the way Tally checks it: party debit (negative) equals
	items + taxes + round off (credits, positive)."""

	def _invoice(self, **kw):
		row = frappe._dict(
			idx=1, item_code="ITEM-A", warehouse="Bay D6 - PI", stock_uom="Box", stock_qty=2.333333, qty=2.333333,
			base_net_amount=976.92, income_account="18% GST Local Credit Sales - PI",
		)
		taxes = [
			frappe._dict(account_head="09% Output CGST - PI", base_tax_amount_after_discount_amount=87.92),
			frappe._dict(account_head="09% Output SGST - PI", base_tax_amount_after_discount_amount=87.92),
		]
		values = {
			"name": "SINV-TEST-1",
			"company": "Pacific Inc",
			"customer": "CUST-1",
			"posting_date": "2026-10-19",
			"items": [row],
			"taxes": taxes,
			"base_rounding_adjustment": 0.24,
			"base_rounded_total": 1153.0,
			"base_grand_total": 1152.76,
		}
		values.update(kw)
		return _Invoice(**values)

	def _build(self, doc, allocations=None):
		values = {
			("Company", "Pacific Inc", "abbr"): "PI",
			("Company", "Pacific Inc", "round_off_account"): "Round Off - PI",
			("Customer", "CUST-1", "customer_name"): "Khus Online Solutions Private Limited",
			("Warehouse", "Bay D6 - PI", "warehouse_name"): "No.256/7 - Bay D6",
			("Item", "ITEM-A", "has_batch_no"): 1,
		}
		with (
			patch.object(frappe, "get_cached_value", side_effect=lambda dt, dn, f: values.get((dt, dn, f))),
			patch.object(T, "piece_unit", return_value=("peices", 9)),
			patch.object(T, "tally_batch_name", side_effect=lambda b, i: b),
			patch.object(T, "_row_allocations", return_value=allocations if allocations is not None else [("Bay D6 - PI", "260808 D-T", 2.333333)]),
		):
			return T.build_voucher_xml(doc)

	def test_balanced_voucher(self):
		xml = self._build(self._invoice())
		self.assertIn("<PARTYLEDGERNAME>Khus Online Solutions Private Limited</PARTYLEDGERNAME>", xml)
		self.assertIn("<REFERENCE>SINV-TEST-1</REFERENCE>", xml)
		self.assertIn("<BILLEDQTY> 2 Box 3 peices</BILLEDQTY>", xml)
		self.assertIn("<GODOWNNAME>No.256/7 - Bay D6</GODOWNNAME><BATCHNAME>260808 D-T</BATCHNAME>", xml)
		self.assertIn("<AMOUNT>-1153.00</AMOUNT>", xml)
		self.assertIn("<LEDGERNAME>09% Output CGST</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>87.92</AMOUNT>", xml)
		self.assertIn("<LEDGERNAME>Round Off</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>0.24</AMOUNT>", xml)

	def test_negative_round_off_is_a_debit(self):
		doc = self._invoice(base_rounding_adjustment=-0.24, base_rounded_total=1152.52, base_grand_total=1152.76)
		xml = self._build(doc)
		self.assertIn("<LEDGERNAME>Round Off</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-0.24</AMOUNT>", xml)

	def test_unbalanced_invoice_is_refused(self):
		with self.assertRaises(T.TallyMappingError):
			self._build(self._invoice(base_rounded_total=2000.0))

	def test_batch_item_without_any_batch_is_refused(self):
		with self.assertRaises(T.TallyMappingError):
			self._build(self._invoice(), allocations=[])

	def test_batches_must_cover_the_billed_qty(self):
		with self.assertRaises(T.TallyMappingError):
			self._build(self._invoice(), allocations=[("Bay D6 - PI", "260808 D-T", 1.0)])

	def test_split_batches_sum_to_the_line_amount(self):
		xml = self._build(
			self._invoice(), allocations=[("Bay D6 - PI", "B1", 1.0), ("Bay D6 - PI", "B2", 1.333333)]
		)
		self.assertIn("<BATCHNAME>B1</BATCHNAME><AMOUNT>418.68</AMOUNT>", xml)
		self.assertIn("<BATCHNAME>B2</BATCHNAME><AMOUNT>558.24</AMOUNT>", xml)

	def test_discount_paisa_drift_goes_to_the_last_line(self):
		# a 5% invoice discount left the lines 1 paisa short of the net total
		doc = self._invoice(base_net_total=976.93, base_rounded_total=1153.01, base_rounding_adjustment=0.24)
		xml = self._build(doc)
		self.assertIn("<ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>18% GST Local Credit Sales</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>976.93</AMOUNT>", xml)
		self.assertIn("<AMOUNT>-1153.01</AMOUNT>", xml)

	def test_discount_on_grand_total_gap_goes_to_round_off(self):
		# ERPNext's grand total is a paisa above net + taxes; its own GL books Round Off 0.25
		doc = self._invoice(base_rounded_total=1153.01, base_rounding_adjustment=0.24)
		xml = self._build(doc)
		self.assertIn("<LEDGERNAME>Round Off</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>0.25</AMOUNT>", xml)

	def test_round_off_without_rounding_adjustment(self):
		doc = self._invoice(base_rounding_adjustment=0, base_rounded_total=0, base_grand_total=1152.77)
		xml = self._build(doc)
		self.assertIn("<LEDGERNAME>Round Off</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>0.01</AMOUNT>", xml)
