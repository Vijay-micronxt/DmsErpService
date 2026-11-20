import json
import sys
import types
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from dms_erp.auth.dealer_api import _dealer_portal_user
from dms_erp.catalog import dealer_catalog_api
from dms_erp.catalog.setup import setup_catalog
from dms_erp.pricing import api as pricing_api
from dms_erp.pricing.setup import setup_pricing
from dms_erp.sales import customer_po, dealer_portal_api
from dms_erp.warehouse.test_fixtures import (
	ensure_company,
	make_dealer,
	make_dealer_addresses,
	make_item,
	make_supplier,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _fake_reader(reply):
	"""A stand-in for pilot_assist.api.ai_api, so no model is called."""
	module = types.ModuleType("pilot_assist.api.ai_api")
	module.read_po_image = lambda file_b64, mime: reply
	module._closest_items = lambda name, limit=3: []
	package = types.ModuleType("pilot_assist")
	api = types.ModuleType("pilot_assist.api")
	api.ai_api = module
	package.api = api
	return patch.dict(sys.modules, {"pilot_assist": package, "pilot_assist.api": api, "pilot_assist.api.ai_api": module})


class TestCompareLines(FrappeTestCase):
	"""The flag rules are pure -- no database, no AI."""

	def test_exact_match_has_no_flags(self):
		po = [{"item": "A", "qty": 5, "boxes": 5}, {"item": "B", "qty": 8, "boxes": 8}]
		self.assertEqual(customer_po.compare_lines([{"item": "A", "qty": 5}, {"item": "B", "qty": 8}], po), [])

	def test_every_kind_of_flag(self):
		inquiry = [{"item": "A", "qty": 5}, {"item": "B", "qty": 8}]
		po = [
			{"item": "A", "qty": 9, "boxes": 9, "code": "A"},
			{"item": "C", "qty": 3, "boxes": 3, "code": "C"},
			{"item": None, "qty": 4, "boxes": 4, "code": "ZZ-1"},
			{"item": None, "qty": None, "code": None, "description": None},
		]
		kinds = sorted(f["type"] for f in customer_po.compare_lines(inquiry, po))
		self.assertEqual(kinds, ["extra", "missing", "qty_diff", "unmatched", "unreadable"])

	def test_rows_for_one_item_are_summed_and_rounding_noise_is_ignored(self):
		po = [{"item": "A", "qty": 2, "boxes": 2, "code": "A"}, {"item": "A", "qty": 3.004, "boxes": 3.004, "code": "A"}]
		self.assertEqual(customer_po.compare_lines([{"item": "A", "qty": 5}], po), [])


class TestParseAiJson(FrappeTestCase):
	def test_plain_and_fenced_json(self):
		body = json.dumps({"po_number": "P1", "lines": [{"code": " X1 ", "description": None, "qty": "12", "unit": "Box"}]})
		for raw in (body, f"```json\n{body}\n```"):
			parsed = customer_po.parse_ai_json(raw)
			self.assertEqual(parsed["po_number"], "P1")
			self.assertEqual(parsed["lines"][0], {"code": "X1", "description": None, "qty": 12.0, "unit": "Box"})

	def test_anything_else_is_not_guessed_at(self):
		for raw in (None, "", "I cannot read this", "[]", '{"lines": "no"}'):
			self.assertIsNone(customer_po.parse_ai_json(raw))

	def test_a_missing_quantity_stays_missing(self):
		parsed = customer_po.parse_ai_json(json.dumps({"lines": [{"code": "A", "qty": None}, {"code": "B", "qty": ""}]}))
		self.assertEqual([row["qty"] for row in parsed["lines"]], [None, None])


class TestSniffMime(FrappeTestCase):
	def test_type_comes_from_the_bytes_not_the_name(self):
		self.assertEqual(customer_po.sniff_mime(b"\xff\xd8\xff\xe0abc"), "image/jpeg")
		self.assertEqual(customer_po.sniff_mime(PNG), "image/png")
		self.assertEqual(customer_po.sniff_mime(b"%PDF-1.7 ..."), "application/pdf")
		with self.assertRaises(frappe.ValidationError):
			customer_po.sniff_mime(b"MZ\x90\x00 an executable renamed po.pdf")


class TestDealerPurchaseOrder(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company()
		setup_catalog()
		setup_pricing()
		supplier = make_supplier("Customer PO Test Supplier")
		cls.dealer = make_dealer("Customer PO Test Dealer")
		cls.dealer_user = _dealer_portal_user(cls.dealer)
		make_dealer_addresses(cls.dealer)
		cls.other_dealer = make_dealer("Customer PO Other Dealer")
		cls.other_user = _dealer_portal_user(cls.other_dealer)
		cls.item_a = make_item("PO-TEST-A", "Vitrified")
		cls.item_b = make_item("PO-TEST-B", "Vitrified")
		for item in (cls.item_a, cls.item_b):
			dealer_catalog_api.set_product_visibility(cls.dealer, item, True)
			pricing_api.ensure_price_record(item, supplier, 300, 20, "2026-08-01")
			pricing_api.approve_price(item=item, final_price=360, reason="Launch")

	def setUp(self):
		frappe.set_user(self.dealer_user)
		self.inquiry = dealer_portal_api.raise_inquiries(
			items=[{"item": self.item_a, "qty": 0, "enteredBoxes": 5}, {"item": self.item_b, "qty": 0, "enteredBoxes": 8}]
		)["enquiry"]
		frappe.set_user("Administrator")
		frappe.db.set_single_value("DMS Sales Settings", "require_po_attachment", 0)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("DMS Sales Settings", "require_po_attachment", 0)

	def _attach(self, content=PNG, name="po.png"):
		with patch("frappe.enqueue") as queued:
			customer_po.attach_po(self.inquiry, name, content, by=self.dealer_user)
		return queued

	def _dpo(self):
		return customer_po.latest_for("inquiry", self.inquiry)

	def _run_check(self, dpo, reply):
		with _fake_reader(reply), patch.object(frappe.db, "commit", lambda *a, **k: None):
			customer_po.run_check(dpo, frappe.db.get_value(customer_po.DPO, dpo, "po_file"))
		return frappe.db.get_value(customer_po.DPO, dpo, "status"), json.loads(frappe.db.get_value(customer_po.DPO, dpo, "check_result"))

	# --- the document -------------------------------------------------------------------------

	def test_attach_creates_a_dealer_purchase_order_linked_to_the_inquiry(self):
		queued = self._attach()
		dpo = self._dpo()
		row = frappe.db.get_value(customer_po.DPO, dpo, ["dealer", "inquiry", "status", "po_file", "attached_by"], as_dict=True)
		self.assertEqual((row.dealer, row.inquiry, row.status, row.attached_by), (self.dealer, self.inquiry, "Attached", self.dealer_user))
		self.assertEqual(frappe.db.get_value("File", row.po_file, ["is_private", "attached_to_doctype", "attached_to_name"]), (1, customer_po.DPO, dpo))
		self.assertTrue(queued.call_args.kwargs["enqueue_after_commit"])

	def test_attaching_again_replaces_the_file_on_the_same_document(self):
		self._attach()
		dpo = self._dpo()
		old = frappe.db.get_value(customer_po.DPO, dpo, "po_file")
		frappe.db.set_value(customer_po.DPO, dpo, {"status": "Reviewed", "reviewed_by": "Administrator"})
		self._attach(PNG + b"1", "second.png")
		self.assertEqual(self._dpo(), dpo)
		self.assertNotEqual(frappe.db.get_value(customer_po.DPO, dpo, "po_file"), old)
		self.assertFalse(frappe.db.exists("File", old))
		self.assertEqual(frappe.db.get_value(customer_po.DPO, dpo, ["status", "reviewed_by"]), ("Attached", None))

	def test_attach_refuses_an_empty_wrong_type_or_oversized_file(self):
		for content in (b"", b"hello", b"%PDF-" + b"0" * customer_po.MAX_PO_BYTES):
			with self.assertRaises(frappe.ValidationError):
				self._attach(content)

	def test_staff_can_add_a_po_with_no_inquiry_and_it_takes_the_linked_documents_dealer(self):
		with patch("frappe.enqueue"):
			loose = customer_po.create_po(self.dealer, "loose.png", PNG, customer_po_number="  DEALER-77 ")
			linked = customer_po.create_po(None, "linked.png", PNG, inquiry=self.inquiry)
		self.assertEqual(frappe.db.get_value(customer_po.DPO, loose, ["inquiry", "customer_po_number"]), (None, "DEALER-77"))
		self.assertEqual(frappe.db.get_value(customer_po.DPO, linked, "dealer"), self.dealer)

	def test_a_po_cannot_be_linked_to_another_dealers_document(self):
		with patch("frappe.enqueue"), self.assertRaises(frappe.ValidationError):
			customer_po.create_po(self.other_dealer, "x.png", PNG, inquiry=self.inquiry)

	def test_a_quotation_made_later_is_recorded_on_the_po(self):
		self._attach()
		customer_po.link_po_to_quotation(frappe._dict({"custom_inquiry": self.inquiry, "name": "QTN-TEST-0001"}))
		self.assertEqual(frappe.db.get_value(customer_po.DPO, self._dpo(), "quotation"), "QTN-TEST-0001")

	def test_staff_can_correct_the_number_and_clear_a_link(self):
		self._attach()
		dpo = self._dpo()
		state = customer_po.update_dealer_po(dpo, {"customer_po_number": "PO/9", "inquiry": ""})
		self.assertEqual((state["customerPoNumber"], state["inquiry"]), ("PO/9", None))
		with self.assertRaises(frappe.ValidationError):
			customer_po.update_dealer_po(dpo, {"dealer": "someone else"})

	def test_list_finds_a_po_by_dealer_name_number_and_linked_ids(self):
		self._attach()
		dpo = self._dpo()
		frappe.db.set_value(customer_po.DPO, dpo, "customer_po_number", "ZXQ-1234")
		for term in ("ZXQ-123", self.inquiry, "Customer PO Test Dealer"):
			self.assertIn(dpo, [row["id"] for row in customer_po.list_dealer_pos(search=term)["items"]])
		self.assertNotIn(dpo, [row["id"] for row in customer_po.list_dealer_pos(status="Matched")["items"]])

	def test_the_panel_lookup_finds_the_po_through_its_inquiry_and_is_empty_when_there_is_none(self):
		self.assertFalse(customer_po.get_dealer_po_for("Inquiry", self.inquiry)["attached"])
		self._attach()
		state = customer_po.get_dealer_po_for("Inquiry", self.inquiry)
		self.assertEqual((state["attached"], state["id"]), (True, self._dpo()))

	# --- dealer view and gate -----------------------------------------------------------------

	def test_the_dealer_view_carries_nothing_from_the_check(self):
		self._attach()
		dpo = self._dpo()
		frappe.db.set_value(customer_po.DPO, dpo, {"status": "Needs review", "check_result": json.dumps({"flags": [{"type": "missing"}]})})
		self.assertEqual(set(customer_po.po_state(self.inquiry)), {"required", "attached", "fileName"})
		frappe.set_user(self.dealer_user)
		self.assertEqual(set(dealer_portal_api.get_my_po(self.inquiry)), {"required", "attached", "fileName"})
		frappe.set_user("Administrator")
		self.assertEqual(customer_po.po_state(self.inquiry, for_staff=True)["status"], "Needs review")

	def test_another_dealer_cannot_read_the_po_state(self):
		frappe.set_user(self.other_user)
		with self.assertRaises(frappe.PermissionError):
			dealer_portal_api.get_my_po(self.inquiry)

	def test_the_gate_is_off_by_default_and_only_asks_for_a_file_when_on(self):
		doc = frappe.get_doc("Inquiry", self.inquiry)
		customer_po.assert_po_attached(doc)  # setting off: no-op
		frappe.db.set_single_value("DMS Sales Settings", "require_po_attachment", 1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			customer_po.assert_po_attached(doc)
		self.assertIn("attach your PO", str(ctx.exception))
		self._attach()
		frappe.db.set_value(customer_po.DPO, self._dpo(), "status", "Needs review")  # the verdict is irrelevant
		customer_po.assert_po_attached(doc)

	def test_dealer_convert_to_order_needs_a_po_only_when_required_and_links_the_order_and_file(self):
		frappe.db.set_single_value("DMS Sales Settings", "require_po_attachment", 1)
		frappe.set_user(self.dealer_user)
		with patch("dms_erp.sales.dealer_portal_api._line_is_available", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				dealer_portal_api.convert_to_order(inquiry=self.inquiry, expected_dispatch=add_days(today(), 5), customer_po="PO-1")
			frappe.set_user("Administrator")
			self._attach()
			frappe.set_user(self.dealer_user)
			order = dealer_portal_api.convert_to_order(inquiry=self.inquiry, expected_dispatch=add_days(today(), 5), customer_po="PO-1")
		frappe.set_user("Administrator")
		dpo = self._dpo()
		self.assertEqual(frappe.db.get_value(customer_po.DPO, dpo, "sales_order"), order["id"])
		po_file = frappe.db.get_value("File", frappe.db.get_value(customer_po.DPO, dpo, "po_file"), ["file_url", "content_hash"], as_dict=True)
		linked = frappe.get_all("File", filters={"attached_to_doctype": "Sales Order", "attached_to_name": order["id"]}, fields=["file_url", "content_hash"])
		self.assertEqual(len(linked), 1)
		self.assertEqual((linked[0].file_url, linked[0].content_hash), (po_file.file_url, po_file.content_hash))
		with self.assertRaises(frappe.ValidationError):  # an order exists now: the file can no longer change
			customer_po.replace_file(dpo, "late.png", PNG)

	# --- the check ----------------------------------------------------------------------------

	def test_the_check_matches_a_correct_po_and_fills_in_the_number_it_read(self):
		self._attach()
		reply = json.dumps({"po_number": "P1", "po_date": "2026-11-18", "lines": [{"code": self.item_a, "qty": 5, "unit": "Box"}, {"code": self.item_b, "qty": 8, "unit": "Boxes"}]})
		status, result = self._run_check(self._dpo(), reply)
		self.assertEqual((status, result["flags"], result["comparedWith"]), ("Matched", [], {"doctype": "Inquiry", "name": self.inquiry}))
		self.assertEqual(frappe.db.get_value(customer_po.DPO, self._dpo(), ["customer_po_number", "flag_count"]), ("P1", 0))

	def test_a_number_staff_typed_is_not_overwritten_by_the_one_read(self):
		self._attach()
		frappe.db.set_value(customer_po.DPO, self._dpo(), "customer_po_number", "TYPED-1")
		self._run_check(self._dpo(), json.dumps({"po_number": "READ-1", "lines": []}))
		self.assertEqual(frappe.db.get_value(customer_po.DPO, self._dpo(), "customer_po_number"), "TYPED-1")

	def test_the_check_flags_differences_and_never_raises_on_a_bad_reply(self):
		self._attach()
		reply = json.dumps({"lines": [{"code": self.item_a, "qty": 9, "unit": "Box"}]})
		status, result = self._run_check(self._dpo(), reply)
		self.assertEqual(status, "Needs review")
		self.assertEqual(sorted(f["type"] for f in result["flags"]), ["missing", "qty_diff"])
		self.assertEqual(frappe.db.get_value(customer_po.DPO, self._dpo(), "flag_count"), 2)
		status, result = self._run_check(self._dpo(), "not json at all")
		self.assertEqual((status, result["flags"][0]["type"]), ("Needs review", "unreadable"))

	def test_a_po_linked_to_nothing_is_only_checked_for_rows_it_cannot_map_or_read(self):
		with patch("frappe.enqueue"):
			dpo = customer_po.create_po(self.dealer, "loose.png", PNG)
		status, result = self._run_check(dpo, json.dumps({"lines": [{"code": self.item_a, "qty": 3, "unit": "Box"}]}))
		self.assertEqual((status, result["comparedWith"]), ("Matched", None))
		status, result = self._run_check(dpo, json.dumps({"lines": [{"code": "ZZ-NO-SUCH-ITEM", "qty": 3, "unit": "Box"}]}))
		self.assertEqual((status, [f["type"] for f in result["flags"]]), ("Needs review", ["unmatched"]))

	def test_a_missing_or_failing_reader_ends_as_unavailable(self):
		self._attach()
		old_reader = types.ModuleType("pilot_assist.api.ai_api")  # has no read_po_image
		package, api = types.ModuleType("pilot_assist"), types.ModuleType("pilot_assist.api")
		api.ai_api, package.api = old_reader, api
		with patch.dict(sys.modules, {"pilot_assist": package, "pilot_assist.api": api, "pilot_assist.api.ai_api": old_reader}), patch.object(frappe.db, "commit", lambda *a, **k: None):
			customer_po.run_check(self._dpo(), frappe.db.get_value(customer_po.DPO, self._dpo(), "po_file"))
		self.assertEqual(frappe.db.get_value(customer_po.DPO, self._dpo(), "status"), "Unavailable")
