import uuid
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dms_erp.warehouse import tally_vouchers as V
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_item,
	make_supplier,
)

ITEM = "TV Test Tile 600x1200"
PLAIN = "TV Test Plain Item"
CUSTOMER = "TV Test Customer"
SUPPLIER = "TV Test Supplier"


def _item(code, has_batch_no):
	# india_compliance makes an HSN code mandatory on every Item, so the shared fixture can't
	# create one on a site that has it installed.
	try:
		return make_item(code, has_batch_no=has_batch_no)
	except frappe.MandatoryError:
		hsn = frappe.get_all("GST HSN Code", limit=1, pluck="name")
		item = frappe.get_doc("Item", code) if frappe.db.exists("Item", code) else frappe.new_doc("Item")
		item.update(
			{
				"item_code": code,
				"item_name": code,
				"item_group": "Vitrified" if frappe.db.exists("Item Group", "Vitrified") else "All Item Groups",
				"stock_uom": "Box",
				"is_stock_item": 1,
				"has_batch_no": has_batch_no,
				"create_new_batch": 0,
				"gst_hsn_code": hsn[0] if hsn else None,
			}
		)
		item.save(ignore_permissions=True)
		return code


def _account(company, ledger, root_type, account_type=None):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"{ledger} - {abbr}"
	if frappe.db.exists("Account", name):
		return name
	parent = frappe.db.get_value("Account", {"company": company, "root_type": root_type, "is_group": 1, "parent_account": ["is", "set"]}) or frappe.db.get_value(
		"Account", {"company": company, "root_type": root_type, "is_group": 1}
	)
	values = {"doctype": "Account", "account_name": ledger, "parent_account": parent, "company": company, "is_group": 0}
	if account_type:
		values["account_type"] = account_type
	return frappe.get_doc(values).insert(ignore_permissions=True).name


def _voucher(vtype, rows, party=None, ledgers=None, **extra):
	party = party or (SUPPLIER if vtype in V.PURCHASE_TYPES else CUSTOMER)
	return {
		"guid": f"test-{uuid.uuid4()}",
		"type": vtype,
		"number": f"T/{uuid.uuid4().hex[:6]}",
		"date": "20260825",
		"party": party,
		"reference": f"REF-{uuid.uuid4().hex[:6]}",
		"ledgers": ledgers if ledgers is not None else [],
		"rows": rows,
		**extra,
	}


def _row(item=ITEM, godown="TV Godown", batch="TV-B1", quantity="10 Box 0 peices", amount=5000, tracking="&#4; Not Applicable", account="TV Sales", group=None, hsn="69072100"):
	return {"item": item, "godown": godown, "batch": batch, "quantity": quantity, "amount": amount, "tracking": tracking, "account": account, "item_group": group, "hsn": hsn}


class TestTallyVouchers(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = ensure_company()
		_item(ITEM, 1)
		_item(PLAIN, 0)
		make_dealer(CUSTOMER)
		make_supplier(SUPPLIER)
		cls.sales_account = _account(cls.company, "TV Sales", "Income")
		cls.purchase_account = _account(cls.company, "TV Purchase", "Expense")
		cls.cgst = _account(cls.company, "TV Output CGST", "Liability", "Tax")
		cls.input_igst = _account(cls.company, "TV Input IGST", "Asset", "Tax")
		cls.wh = frappe.db.get_value("Warehouse", {"warehouse_name": "TV Godown", "company": cls.company}) or (
			frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "TV Godown", "company": cls.company}).insert(ignore_permissions=True).name
		)
		cls.batch = "TV-B1" if frappe.db.exists("Batch", "TV-B1") else frappe.get_doc({"doctype": "Batch", "batch_id": "TV-B1", "item": ITEM}).insert(ignore_permissions=True).name

	def _run(self, vouchers):
		# Tally writes "10 Box 0 peices"; reading it needs the item's pieces-per-box, which a test item has none of.
		with patch.object(V, "TALLY_VOUCHERS_ENABLED", True), patch.object(V, "piece_unit", return_value=("peices", 9)):
			return V.receive_tally_vouchers(vouchers, company=self.company)

	def _doc(self, out, index=0):
		made = out["created"][index]
		return frappe.get_doc(made["doctype"], made["name"])

	def test_refuses_while_disabled(self):
		# This site may have the import switched on (a developer's local one does), so turn it off explicitly.
		with patch.dict(frappe.local.conf, {"dms_erp_tally_vouchers_enabled": 0}), self.assertRaises(frappe.ValidationError):
			V.receive_tally_vouchers([_voucher("Purchase", [_row()])])

	def test_a_delivery_note_makes_a_draft_delivery_note(self):
		out = self._run([_voucher("Delivery Note", [_row(quantity="4 Box 0 peices", amount=2000)])])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.docstatus, doc.customer), ("Delivery Note", 0, CUSTOMER))
		row = doc.items[0]
		self.assertEqual((row.item_code, row.warehouse, row.batch_no, row.qty, row.rate), (ITEM, self.wh, self.batch, 4, 500))
		self.assertTrue(doc.tally_guid.startswith("test-"))

	def test_a_direct_sale_is_an_invoice_that_also_moves_stock_and_ties_to_tally(self):
		ledgers = [
			{"ledger": "Some Party", "amount": "-5900", "party": True},
			{"ledger": "TV Output CGST", "amount": "900", "party": False},
		]
		out = self._run([_voucher("Sales", [_row()], ledgers=ledgers)])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.docstatus, doc.update_stock, doc.customer), ("Sales Invoice", 0, 1, CUSTOMER))
		self.assertEqual(doc.items[0].income_account, self.sales_account)
		# a sale's tax ledger is a credit in Tally (positive) and stays positive here
		self.assertEqual((doc.taxes[0].account_head, doc.taxes[0].charge_type, doc.taxes[0].tax_amount), (self.cgst, "Actual", 900))
		made = out["created"][0]
		self.assertEqual((made["tallyTotal"], made["difference"]), (5900, 0))

	def test_a_sale_against_a_delivery_note_moves_no_stock(self):
		out = self._run([_voucher("Sales", [_row(tracking="4415/26-27")])])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		self.assertEqual(self._doc(out).update_stock, 0)
		self.assertFalse(out["created"][0]["stockUpdate"])

	def test_a_sale_part_against_a_delivery_note_is_left_for_a_person(self):
		out = self._run([_voucher("Sales", [_row(tracking="4415/26-27"), _row()])])
		self.assertEqual(out["created"], [])
		self.assertIn("check this one in Tally", out["unplaced"][0]["reason"])

	def test_a_purchase_is_an_invoice_that_brings_stock_in(self):
		ledgers = [
			{"ledger": "Some Party", "amount": "5900", "party": True},
			{"ledger": "TV Input IGST", "amount": "-900", "party": False},
		]
		out = self._run([_voucher("Purchase (New)", [_row(account="TV Purchase")], ledgers=ledgers)])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		doc = self._doc(out)
		self.assertEqual((doc.doctype, doc.docstatus, doc.update_stock, doc.supplier), ("Purchase Invoice", 0, 1, SUPPLIER))
		# the supplier's own bill number is Tally's reference
		self.assertEqual(doc.bill_no, doc.tally_reference)
		self.assertEqual(doc.items[0].expense_account, self.purchase_account)
		# a purchase's tax ledger is a debit in Tally (negative); the ERP wants it positive
		self.assertEqual(doc.taxes[0].tax_amount, 900)
		self.assertEqual(out["created"][0]["difference"], 0)

	def test_a_purchase_creates_the_item_and_batch_it_brings_and_says_so(self):
		new_item = f"TV New Item {uuid.uuid4().hex[:5]} 09 Pcs"
		out = self._run([_voucher("Purchase", [_row(item=new_item, batch="TV-NEWLOT", account="TV Purchase", group="TV New Group")])])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		self.assertEqual(out["newItems"], [{"item": new_item, "group": "TV New Group", "hsn": "69072100", "needsHsn": False}])
		self.assertEqual(out["newBatches"], [{"batch": "TV-NEWLOT", "item": new_item}])
		item = frappe.get_doc("Item", new_item)
		self.assertEqual((item.item_group, item.stock_uom, item.has_batch_no, item.is_stock_item), ("TV New Group", "Box", 1, 1))
		self.assertEqual(item.gst_hsn_code, "69072100")
		self.assertIn(("peices", round(1 / 9, 6)), [(u.uom, round(u.conversion_factor, 6)) for u in item.uoms])

	def test_a_new_item_with_a_four_digit_hsn_is_bought_but_not_sellable_until_fixed(self):
		new_item = f"TV Four Digit {uuid.uuid4().hex[:5]}"
		out = self._run([_voucher("Purchase", [_row(item=new_item, account="TV Purchase", hsn="6907")])])
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		self.assertEqual(out["newItems"][0]["needsHsn"], True)
		item = frappe.get_doc("Item", new_item)
		self.assertEqual((item.is_sales_item, item.gst_hsn_code), (0, None))

	def test_boxes_with_no_loose_pieces_read_on_an_item_that_has_no_pieces_per_box(self):
		new_item = f"TV No Pieces {uuid.uuid4().hex[:5]}"
		with patch.object(V, "TALLY_VOUCHERS_ENABLED", True), patch.object(V, "piece_unit", return_value=None):
			out = V.receive_tally_vouchers([_voucher("Purchase", [_row(item=new_item, quantity="60 Box 0 peices", account="TV Purchase")])], company=self.company)
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		self.assertEqual(self._doc(out).items[0].qty, 60)

	def test_transport_and_e_invoice_details_go_into_the_documents_own_fields(self):
		transport = {"vehicle": "KA53HJ6190", "eway_bill": "112525073486", "irn": "ad221f45", "irn_ack_no": "1126", "irn_ack_date": "20260821", "transporter": "Trust Logistic", "lr_no": "LR-9", "entered_by": "angeline", "driver_name": "Saddam", "bogus": "ignored"}
		out = self._run(
			[
				_voucher("Sales", [_row()], transport=transport),
				_voucher("Delivery Note", [_row()], transport=transport),
				_voucher("Purchase", [_row(account="TV Purchase")], transport={**transport, "vehicle": "   "}),
			]
		)
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		sale, note, purchase = (self._doc(out, i) for i in range(3))
		self.assertEqual((sale.tally_vehicle_no, sale.tally_eway_bill_no, sale.tally_irn, sale.tally_irn_ack_no, sale.tally_transporter, sale.tally_entered_by), ("KA53HJ6190", "112525073486", "ad221f45", "1126", "Trust Logistic", "angeline"))
		# a Delivery Note has no driver fields: it simply gets the ones it has, and nothing fails
		self.assertEqual((note.tally_vehicle_no, note.tally_transporter, note.tally_irn, note.meta.has_field("tally_driver_name")), ("KA53HJ6190", "Trust Logistic", "ad221f45", False))
		# the driver's details are on a Purchase Invoice only; a blank value stays blank
		self.assertEqual((purchase.tally_driver_name, purchase.tally_vehicle_no, sale.get("tally_driver_name")), ("Saddam", None, None))

	def test_the_line_shows_boxes_and_pieces_as_tally_does_next_to_the_decimal_qty(self):
		out = self._run(
			[
				_voucher("Delivery Note", [_row(quantity="16 Box 3 peices", amount=1600)]),
				_voucher("Delivery Note", [_row(quantity=" 7 Box 0 peices", amount=700)]),
			]
		)
		self.assertEqual(out["unplacedTotal"], 0, out["unplaced"])
		first, second = self._doc(out, 0).items[0], self._doc(out, 1).items[0]
		self.assertEqual((first.tally_qty_text, round(first.qty, 4), round(first.tally_qty_value, 4)), ("16 Box 3 Pcs", round(16 + 3 / 9, 4), round(16 + 3 / 9, 4)))
		self.assertEqual((second.tally_qty_text, second.qty), ("7 Box 0 Pcs", 7))

	def test_a_sale_never_invents_an_item_or_batch(self):
		out = self._run(
			[
				_voucher("Sales", [_row(item="No Such Item")]),
				_voucher("Sales", [_row(batch="NEW-BATCH")]),
			]
		)
		self.assertEqual((out["created"], out["unplacedTotal"]), ([], 2))
		self.assertFalse(frappe.db.exists("Item", "No Such Item"))
		self.assertFalse(frappe.db.exists("Batch", "NEW-BATCH"))

	def test_one_voucher_missing_a_master_is_not_made_but_the_rest_are(self):
		out = self._run(
			[
				_voucher("Sales", [_row()], party="No Such Customer"),
				_voucher("Purchase", [_row(account="TV Purchase")], party="No Such Supplier"),
				_voucher("Sales", [_row(godown="No Such Godown")]),
				_voucher("Sales", [_row(account="No Such Ledger")]),
				_voucher("Sales", [_row(quantity="garbage")]),
				_voucher("Sales", [_row(quantity="1 Box 0 peices")]),
			]
		)
		self.assertEqual(len(out["created"]), 1)
		self.assertEqual(out["unplacedTotal"], 5)
		reasons = " | ".join(u["reason"] for u in out["unplaced"])
		for needle in ("Customer not in the ERP", "Supplier not in the ERP", "Godown not in the ERP", "Account not in the ERP"):
			self.assertIn(needle, reasons)

	def test_an_item_or_batch_made_for_a_voucher_that_then_fails_is_not_left_behind(self):
		new_item = f"TV Orphan {uuid.uuid4().hex[:5]}"
		out = self._run([_voucher("Purchase", [_row(item=new_item, account="TV Purchase"), _row(godown="No Such Godown", account="TV Purchase")])])
		self.assertEqual((out["created"], out["newItems"], out["newBatches"]), ([], [], []))
		self.assertFalse(frappe.db.exists("Item", new_item))

	def test_what_the_erp_already_holds_is_left_alone(self):
		first = _voucher("Delivery Note", [_row()])
		self.assertEqual(len(self._run([first])["created"]), 1)
		again = self._run([first])
		self.assertEqual((again["created"], again["alreadyInErp"]), ([], 1))
		with patch.object(V, "TALLY_VOUCHERS_ENABLED", True):
			self.assertEqual(V.filter_new_tally_vouchers([first["guid"], "brand-new"]), ["brand-new"])

	def test_cancelled_and_unhandled_types_are_counted_not_made(self):
		out = self._run(
			[
				_voucher("Purchase", [_row()], cancelled=True),
				_voucher("Journal", [_row()]),
				_voucher("Stock Journal", [_row()]),
			]
		)
		self.assertEqual(out["created"], [])
		self.assertEqual(out["ignored"]["Cancelled or optional"], 1)
		self.assertEqual(out["ignored"]["Journal vouchers are not imported yet."], 1)
		self.assertEqual(out["ignored"]["Stock Journal vouchers are not imported yet."], 1)

	def test_the_same_voucher_twice_in_one_request_is_made_once(self):
		v = _voucher("Delivery Note", [_row()])
		self.assertEqual(len(self._run([v, dict(v)])["created"]), 1)

	def test_nothing_is_ever_submitted(self):
		out = self._run([_voucher("Delivery Note", [_row()]), _voucher("Sales", [_row()]), _voucher("Purchase", [_row(account="TV Purchase")])])
		self.assertEqual({self._doc(out, i).docstatus for i in range(len(out["created"]))}, {0})
