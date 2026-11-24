"""Dealer Purchase Order: the customer's PO (a photo, scan or PDF) as a document of its own, checked
in the background against what was asked for, and linked to the Inquiry, Quotation and Sales Order
it belongs to.

Why a document of its own, not fields on the Inquiry: a PO is a record Pacific keeps -- it can arrive
before any inquiry exists, from the dealer portal, by staff, or over WhatsApp; it can be replaced; it
is reviewed; and one PO links forward through the whole chain (Inquiry -> Quotation -> Sales Order).
Hanging it on one of those documents would lose it from the others.

Three pieces, deliberately separate so none of them can break another:

1. Attach -- validate the file, store it as a *private* File on the Dealer Purchase Order, queue the
   check. Quick, synchronous, never talks to an AI.
2. Check (run_check) -- background job. Reads the PO through pilot_assist's read_po_image, maps each
   printed row to an Item, compares with the linked document's lines and writes flags. Every failure
   (pilot_assist missing, no key, unreadable file, bad JSON) ends as a status staff can see
   ("Unavailable" / "Needs review"); nothing here ever raises into a request.
3. Gate (assert_po_attached) -- the one behaviour change for the dealer: no PO, no order. Off unless
   DMS Sales Settings > require_po_attachment is ticked, and it never looks at the check's outcome:
   the AI result is for staff review, not a condition a dealer can fail.

What the check finds is staff-only. Nothing in `check_result` or the flags is returned by a
dealer-facing endpoint (see dealer_portal_api.get_my_po) -- the same boundary as
sales.utils.safe_customer_message.

Linking is by hooks (link_po_to_quotation, link_po_to_order), not by edits to quotation_api/order_api,
so every creation path -- staff, dealer portal, WhatsApp, approvals replay -- links the same way.
"""

import base64
import contextlib
import json
import os
import re

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime

DPO = "Dealer Purchase Order"
MAX_PO_BYTES = 10 * 1024 * 1024
# A PO stays replaceable until an order has been made from it: that order already carries its own
# link to the file, and swapping the file underneath it would leave the two disagreeing.
CLOSED_INQUIRY_STATUSES = {"Converted to Order", "Rejected", "Closed"}
# Qty differences smaller than this are the AI reading "12.0" and the inquiry holding 12.
QTY_TOLERANCE = 0.01
# A name-only match (no code printed, or an unknown one) must be this similar to count as the item.
FUZZY_MATCH_MIN_SCORE = 0.8

# What a PO prints next to a quantity, mapped to catalog.utils.ORDER_UOMS. Matched after dropping
# spaces and dots ("Sq. Ft" -> "sqft"). Anything else, or no unit at all, is read as boxes -- the
# unit a dealer's PO for tiles is almost always in.
_UNIT_ALIASES = {
	"box": "Box", "boxes": "Box", "bx": "Box", "ctn": "Box", "carton": "Box", "cartons": "Box",
	"pcs": "Pcs", "pc": "Pcs", "piece": "Pcs", "pieces": "Pcs", "nos": "Pcs", "no": "Pcs",
	"sqft": "Sqft", "sqfeet": "Sqft", "sqfoot": "Sqft",
	"sqm": "Sqm", "sqmtr": "Sqm", "sqmeter": "Sqm", "sqmetre": "Sqm",
	"kg": "Kg", "kgs": "Kg",
}


def po_required() -> bool:
	return bool(cint(frappe.db.get_single_value("DMS Sales Settings", "require_po_attachment")))


def sniff_mime(content: bytes) -> str:
	"""The file's real type from its first bytes, not from the name or the browser's claim --
	only a JPG, PNG or PDF is ever stored or sent to a model."""
	if content[:3] == b"\xff\xd8\xff":
		return "image/jpeg"
	if content[:8] == b"\x89PNG\r\n\x1a\n":
		return "image/png"
	if content[:5] == b"%PDF-":
		return "application/pdf"
	frappe.throw(_("Please upload the PO as a JPG, PNG or PDF file."), frappe.ValidationError)


def _validate_upload(content: bytes) -> None:
	if not content:
		frappe.throw(_("The file is empty."), frappe.ValidationError)
	if len(content) > MAX_PO_BYTES:
		frappe.throw(_("The file is too large. Please keep it under {0} MB.").format(MAX_PO_BYTES // (1024 * 1024)), frappe.ValidationError)
	sniff_mime(content)


def latest_for(field: str, name: str) -> str | None:
	"""The newest Dealer Purchase Order linked to an Inquiry / Quotation / Sales Order."""
	if not name:
		return None
	return frappe.db.get_value(DPO, {field: name}, "name", order_by="creation desc")


# --- creating and replacing ------------------------------------------------------------------


def _store_file(dpo: str, filename: str, content: bytes) -> str:
	"""Saves the bytes as a private File attached to the Dealer Purchase Order; the old file, if any,
	is dropped. Returns the new File's name."""
	from frappe.utils.file_manager import save_file

	safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename or "") or "po")[:80] or "po"
	previous = frappe.db.get_value(DPO, dpo, "po_file")
	file_doc = save_file(f"PO-{safe_name}", content, DPO, dpo, is_private=1)
	# set_value, not doc.save: validate/on_update has nothing to add for what is only a file link,
	# and a save would bump the PO's timestamps for a check status change.
	frappe.db.set_value(
		DPO,
		dpo,
		{
			"po_file": file_doc.name,
			"file_name": file_doc.file_name,
			"status": "Attached",
			"check_result": None,
			"check_summary": None,
			"flag_count": 0,
			"checked_on": None,
			"reviewed_by": None,
			"reviewed_on": None,
		},
		update_modified=False,
	)
	if previous and previous != file_doc.name and frappe.db.exists("File", previous):
		frappe.delete_doc("File", previous, ignore_permissions=True, force=True)
	return file_doc.name


def _queue_check(dpo: str, file: str) -> None:
	frappe.enqueue(
		"dms_erp.sales.customer_po.run_check",
		queue="default",
		timeout=300,
		enqueue_after_commit=True,
		dpo=dpo,
		file=file,
	)


def _autolink(dpo: str) -> None:
	"""Fills the links that are already knowable from the ones given: a PO attached to an inquiry that
	already has a quotation or an order belongs to those too. Never overwrites a link that is set."""
	row = frappe.db.get_value(DPO, dpo, ["inquiry", "quotation", "sales_order"], as_dict=True)
	if not row or not row.inquiry:
		return
	updates = {}
	if not row.quotation:
		quotation = frappe.db.get_value("Quotation", {"custom_inquiry": row.inquiry, "docstatus": ["<", 2]}, "name", order_by="creation desc")
		if quotation:
			updates["quotation"] = quotation
	if not row.sales_order:
		order = frappe.db.get_value("Inquiry", row.inquiry, "linked_sales_order")
		if order:
			updates["sales_order"] = order
	if updates:
		frappe.db.set_value(DPO, dpo, updates, update_modified=False)


def create_po(
	dealer: str | None,
	filename: str,
	content: bytes,
	by: str | None = None,
	inquiry: str | None = None,
	quotation: str | None = None,
	sales_order: str | None = None,
	customer_po_number: str | None = None,
) -> str:
	"""The unguarded core: the caller has already decided this user may create a PO for this dealer.
	The dealer defaults to the linked document's own. Returns the new Dealer Purchase Order's name."""
	_validate_upload(content)
	if not dealer:
		for doctype, name, field in (("Inquiry", inquiry, "dealer"), ("Quotation", quotation, "party_name"), ("Sales Order", sales_order, "customer")):
			if name:
				dealer = frappe.db.get_value(doctype, name, field)
				break
	if not dealer:
		frappe.throw(_("Choose the dealer this PO is from."), frappe.ValidationError)
	# A PO number the dealer already gave on the inquiry is the PO's number too, unless one is given here.
	if inquiry and not (customer_po_number or "").strip():
		customer_po_number = frappe.db.get_value("Inquiry", inquiry, "customer_po")
	doc = frappe.get_doc(
		{
			"doctype": DPO,
			"dealer": dealer,
			"inquiry": inquiry or None,
			"quotation": quotation or None,
			"sales_order": sales_order or None,
			"customer_po_number": (customer_po_number or "").strip() or None,
			"status": "Attached",
			"attached_by": by or frappe.session.user,
			"attached_on": now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	file_name = _store_file(doc.name, filename, content)
	_autolink(doc.name)
	if frappe.db.get_value(DPO, doc.name, "sales_order"):
		_link_file_to("Sales Order", frappe.db.get_value(DPO, doc.name, "sales_order"), file_name)
	_queue_check(doc.name, file_name)
	return doc.name


def replace_file(dpo: str, filename: str, content: bytes, by: str | None = None) -> str:
	"""A new file for an existing PO: re-checked from scratch, review cleared."""
	_validate_upload(content)
	if frappe.db.get_value(DPO, dpo, "sales_order"):
		frappe.throw(_("An order has already been made from this PO, so its file can no longer be changed."), frappe.ValidationError)
	file_name = _store_file(dpo, filename, content)
	frappe.db.set_value(DPO, dpo, {"attached_by": by or frappe.session.user, "attached_on": now_datetime()}, update_modified=False)
	_queue_check(dpo, file_name)
	return file_name


def attach_po(inquiry: str, filename: str, content: bytes, by: str | None = None) -> dict:
	"""What the dealer portal does: put a PO on one inquiry -- the inquiry's existing PO gets the new
	file, otherwise a Dealer Purchase Order is created for it. Returns only whether a PO is on file
	and under what name (the dealer's whole view)."""
	status = frappe.db.get_value("Inquiry", inquiry, "status")
	if status in CLOSED_INQUIRY_STATUSES:
		frappe.throw(_("Inquiry {0} is {1}, so its PO can no longer be changed.").format(inquiry, status), frappe.ValidationError)
	existing = latest_for("inquiry", inquiry)
	if existing:
		replace_file(existing, filename, content, by=by)
	else:
		existing = create_po(None, filename, content, by=by, inquiry=inquiry)
	return {"attached": True, "fileName": frappe.db.get_value(DPO, existing, "file_name")}


def assert_po_attached(inquiry_doc) -> None:
	"""The gate. A no-op unless the setting is on; never reads the AI check's outcome."""
	if not po_required():
		return
	dpo = latest_for("inquiry", inquiry_doc.name)
	file_name = frappe.db.get_value(DPO, dpo, "po_file") if dpo else None
	if not file_name or not frappe.db.exists("File", file_name):
		frappe.throw(_("Please attach your PO before placing the order."), frappe.ValidationError)


# --- linking forward: Quotation and Sales Order ----------------------------------------------


def _link_file_to(doctype: str, name: str, file_name: str) -> None:
	"""Links the PO's stored file onto another document as a second File row on the same stored file
	(content_hash kept, so deleting one row never removes the file the other still needs). Idempotent."""
	src = frappe.db.get_value("File", file_name, ["file_name", "file_url", "is_private", "content_hash"], as_dict=True)
	if not src or frappe.db.exists("File", {"attached_to_doctype": doctype, "attached_to_name": name, "file_url": src.file_url}):
		return
	frappe.get_doc(
		{
			"doctype": "File",
			"file_name": src.file_name,
			"file_url": src.file_url,
			"is_private": src.is_private,
			"content_hash": src.content_hash,
			"attached_to_doctype": doctype,
			"attached_to_name": name,
		}
	).insert(ignore_permissions=True)


def link_po_to_quotation(doc, method=None) -> None:
	"""doc_events: Quotation after_insert. A quotation made from an inquiry that has a PO belongs to
	that PO. Wrapped so a failure here can never stop a quotation being created."""
	try:
		inquiry = doc.get("custom_inquiry")
		dpo = latest_for("inquiry", inquiry)
		if dpo and not frappe.db.get_value(DPO, dpo, "quotation"):
			frappe.db.set_value(DPO, dpo, "quotation", doc.name, update_modified=False)
	except Exception:  # noqa: BLE001 -- by design: a failed link must never stop a quotation being created
		frappe.log_error(title="Dealer PO: could not link the PO to the Quotation")


def link_po_to_order(doc, method=None) -> None:
	"""doc_events: Sales Order after_insert. An order made from an inquiry or a quotation belongs to
	that chain's PO: the PO records the order, and the same file is linked onto the order. Wrapped so a
	failure here can never stop an order being created."""
	try:
		source_type, source_ref = doc.get("custom_source_type"), doc.get("custom_source_ref")
		dpo = None
		if source_type == "Inquiry":
			dpo = latest_for("inquiry", source_ref)
		elif source_type == "Quotation":
			dpo = latest_for("quotation", source_ref)
			if not dpo:
				dpo = latest_for("inquiry", frappe.db.get_value("Quotation", source_ref, "custom_inquiry"))
		if not dpo:
			return
		updates = {}
		if not frappe.db.get_value(DPO, dpo, "sales_order"):
			updates["sales_order"] = doc.name
		if source_type == "Quotation" and not frappe.db.get_value(DPO, dpo, "quotation"):
			updates["quotation"] = source_ref
		if updates:
			frappe.db.set_value(DPO, dpo, updates, update_modified=False)
		file_name = frappe.db.get_value(DPO, dpo, "po_file")
		if file_name:
			_link_file_to("Sales Order", doc.name, file_name)
	except Exception:  # noqa: BLE001 -- by design: a failed link must never stop an order being created
		frappe.log_error(title="Dealer PO: could not link the PO to the Sales Order")


# --- the background check ---------------------------------------------------------------------


def run_check(dpo: str, file: str) -> None:
	"""Background job. Runs as Administrator: it is system processing of a file the PO already holds,
	and the item matching must see the whole catalogue whatever user attached the file."""
	frappe.set_user("Administrator")
	if frappe.db.get_value(DPO, dpo, "po_file") != file:
		return  # replaced or removed while this job waited in the queue
	frappe.db.set_value(DPO, dpo, "status", "Checking", update_modified=False)
	frappe.db.commit()
	try:
		result = _read_and_compare(dpo, file)
	except Exception:  # noqa: BLE001 -- by design: whatever goes wrong becomes a status staff can see
		frappe.log_error(title=f"Dealer PO check failed ({dpo})")
		result = _result("Unavailable", reason="The automatic check could not be completed.")
	if frappe.db.get_value(DPO, dpo, "po_file") != file:
		return
	updates = {
		"status": result["status"],
		"check_result": json.dumps(result, default=str),
		"check_summary": _summary(result),
		"flag_count": len(result["flags"]),
		"checked_on": now_datetime(),
	}
	# What was read off the PO fills what staff have not already typed -- never overwrites it.
	row = frappe.db.get_value(DPO, dpo, ["customer_po_number", "po_date"], as_dict=True)
	if not row.customer_po_number and result.get("poNumber"):
		updates["customer_po_number"] = str(result["poNumber"])[:140]
	if not row.po_date and result.get("poDate"):
		with contextlib.suppress(Exception):  # a date the model got wrong is simply not used
			updates["po_date"] = getdate(result["poDate"])
	frappe.db.set_value(DPO, dpo, updates, update_modified=False)
	frappe.db.commit()


def _summary(result: dict) -> str:
	"""A few plain lines for the Desk form and list -- the staff app builds its own from the flags."""
	if result["status"] == "Unavailable":
		return result.get("reason") or "The automatic check could not run."
	if not result["flags"]:
		basis = result.get("comparedWith")
		return f"Matches {basis['doctype']} {basis['name']}." if basis else "Read; nothing to compare it with yet."
	counts: dict[str, int] = {}
	for flag in result["flags"]:
		counts[flag["type"]] = counts.get(flag["type"], 0) + 1
	return ", ".join(f"{n} {kind.replace('_', ' ')}" for kind, n in counts.items())


def _file_bytes(file_name: str) -> bytes:
	"""The stored bytes of a File, read as bytes: File.get_content() hands back text whenever the
	content happens to decode as UTF-8, which would corrupt a binary file."""
	file_doc = frappe.get_doc("File", file_name)
	url = file_doc.file_url or ""
	if S3_DOWNLOAD_MARKER in url:
		return _s3_bytes(url, file_doc.file_name)
	with open(file_doc.get_full_path(), "rb") as handle:
		return handle.read()


S3_DOWNLOAD_MARKER = "frappe_s3_attachment.controller.generate_file"


def _s3_bytes(url: str, file_name: str) -> bytes:
	"""With frappe_s3_attachment installed, the upload hook moves the file to S3, deletes the local copy
	and rewrites File.file_url to its download endpoint -- so there is nothing on disk to open. Ask the
	same app for a short-lived signed link and fetch it (needs s3:GetObject on the bucket)."""
	from urllib.parse import parse_qs, urlparse
	from urllib.request import urlopen

	from frappe_s3_attachment.controller import S3Operations

	key = parse_qs(urlparse(url).query).get("key", [""])[0]
	if not key:
		frappe.throw(_("The stored file has no S3 key."))
	signed = S3Operations().get_url(key, file_name)
	with urlopen(signed, timeout=30) as response:
		return response.read()


def _result(status: str, **extra) -> dict:
	return {"status": status, "checkedOn": str(now_datetime()), "poNumber": None, "lines": [], "flags": [], **extra}


def _basis(dpo_doc) -> tuple[str, str, list[dict]] | None:
	"""What the PO is checked against: its inquiry, else its quotation, else its order -- the
	furthest-back document that has items. None for a PO linked to nothing yet."""
	for field, doctype in (("inquiry", "Inquiry"), ("quotation", "Quotation"), ("sales_order", "Sales Order")):
		name = dpo_doc.get(field)
		if not name or not frappe.db.exists(doctype, name):
			continue
		doc = frappe.get_doc(doctype, name)
		by_item: dict[str, float] = {}
		for row in doc.items:
			code = row.item if doctype == "Inquiry" else row.item_code
			if code:
				by_item[code] = by_item.get(code, 0) + flt(row.qty)
		return doctype, name, [{"item": item, "qty": qty} for item, qty in by_item.items()]
	return None


def _read_and_compare(dpo: str, file: str) -> dict:
	try:
		from pilot_assist.api import ai_api
	except ImportError:
		return _result("Unavailable", reason="The AI reader is not installed on this site; review the PO by eye.")
	reader = getattr(ai_api, "read_po_image", None)
	if reader is None:
		return _result("Unavailable", reason="The installed AI reader is too old to read a PO; review the PO by eye.")

	content = _file_bytes(file)
	raw = reader(base64.b64encode(content).decode("ascii"), sniff_mime(content))
	data = parse_ai_json(raw)
	if data is None:
		return _result("Needs review", flags=[{"type": "unreadable", "note": "The PO could not be read automatically."}])

	dpo_doc = frappe.get_doc(DPO, dpo)
	closest = getattr(ai_api, "_closest_items", None)
	po_lines = [_match_line(line, dpo_doc.dealer, closest) for line in data["lines"]]
	basis = _basis(dpo_doc)
	flags = compare_lines(basis[2], po_lines) if basis else compare_lines([], po_lines, expect_items=False)
	return _result(
		"Needs review" if flags else "Matched",
		poNumber=data.get("po_number"),
		poDate=data.get("po_date"),
		lines=po_lines,
		flags=flags,
		comparedWith={"doctype": basis[0], "name": basis[1]} if basis else None,
	)


def parse_ai_json(raw: str | None) -> dict | None:
	"""The model's reply as {"po_number", "po_date", "lines": [...]}, or None when it is not that.
	Tolerates the markdown fence some models add despite being told not to; anything else that
	does not parse is not guessed at."""
	text = (raw or "").strip()
	if text.startswith("```"):
		text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
	try:
		data = json.loads(text)
	except ValueError:
		return None
	if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
		return None
	lines = []
	for row in data["lines"]:
		if not isinstance(row, dict):
			continue
		lines.append(
			{
				"code": (str(row["code"]).strip() or None) if row.get("code") is not None else None,
				"description": (str(row["description"]).strip() or None) if row.get("description") is not None else None,
				"qty": flt(row["qty"]) if isinstance(row.get("qty"), (int, float, str)) and str(row["qty"]).strip() != "" else None,
				"unit": (str(row["unit"]).strip() or None) if row.get("unit") is not None else None,
			}
		)
	return {"po_number": data.get("po_number"), "po_date": data.get("po_date"), "lines": lines}


def _match_line(line: dict, dealer: str, closest=None) -> dict:
	"""One printed PO row -> the Item it most likely is, and how it was found. Exact item code first,
	then the dealer's own code for the item (what a dealer's PO normally carries), then -- only if
	nothing printed matches -- the closest item name, accepted at FUZZY_MATCH_MIN_SCORE or better."""
	code, name = line.get("code"), line.get("description")
	item = matched_by = score = None
	if code and frappe.db.exists("Item", code):
		item, matched_by = code, "item code"
	elif code:
		item = frappe.db.get_value("Item Dealer Code", {"dealer": dealer, "customer_item_code": code}, "parent")
		matched_by = "your item code" if item else None
	if not item and closest and (name or code):
		try:
			best = next(iter(closest(name or code, 3)), None)
		except Exception:  # noqa: BLE001 -- the fuzzy lookup is a best-effort extra; a failure just means no name match
			best = None
		if best and best.get("score", 0) >= FUZZY_MATCH_MIN_SCORE:
			item, matched_by, score = best["name"], "name", round(best["score"], 2)
	return {**line, "item": item, "matchedBy": matched_by, "score": score, "boxes": _qty_in_boxes(item, line)}


def _qty_in_boxes(item: str | None, line: dict) -> float | None:
	"""The row's quantity in boxes, converted with the same rule order creation uses (rounded up).
	None when the quantity is unreadable; the plain number when the item is unknown or has no
	per-box figure for that unit, since nothing better can be said."""
	qty = line.get("qty")
	if qty is None:
		return None
	unit = _UNIT_ALIASES.get(re.sub(r"[\s.]", "", (line.get("unit") or "").lower()), "Box")
	if item and unit != "Box":
		from dms_erp.catalog.utils import convert_qty_to_boxes

		try:
			return convert_qty_to_boxes(item, qty, unit)
		except frappe.ValidationError:
			return qty
	return qty


def compare_lines(basis_lines: list[dict], po_lines: list[dict], expect_items: bool = True) -> list[dict]:
	"""The review flags. Pure (no database), so the rules are testable on their own.

	missing   an item on the linked document that no PO row maps to
	qty_diff  both name the item but not the same number of boxes (PO rows for one item are summed)
	extra     a PO row that maps to an item the linked document does not have
	unmatched a PO row that maps to no item at all -- an unfamiliar code, or one misread
	unreadable a PO row with no quantity, or none of code/description

	`expect_items=False` is a PO linked to nothing yet: there is nothing to be missing or extra, so only
	the rows that cannot be mapped or read are flagged. Flags are for a person to look at. None of
	them stops an order."""
	flags = []
	po_boxes: dict[str, float] = {}
	for line in po_lines:
		item = line.get("item")
		printed = line.get("code") or line.get("description")
		if line.get("qty") is None or not printed:
			flags.append({"type": "unreadable", "printed": printed, "note": "This PO row could not be read clearly."})
			continue
		if not item:
			flags.append({"type": "unmatched", "printed": printed, "poQty": line["qty"], "unit": line.get("unit")})
			continue
		po_boxes[item] = po_boxes.get(item, 0) + flt(line.get("boxes") if line.get("boxes") is not None else line["qty"])

	if not expect_items:
		return flags
	asked = {row["item"]: flt(row["qty"]) for row in basis_lines}
	for item, qty in asked.items():
		if item not in po_boxes:
			flags.append({"type": "missing", "item": item, "inquiryQty": qty})
		elif abs(po_boxes[item] - qty) > QTY_TOLERANCE:
			flags.append({"type": "qty_diff", "item": item, "inquiryQty": qty, "poQty": po_boxes[item]})
	for item, qty in po_boxes.items():
		if item not in asked:
			flags.append({"type": "extra", "item": item, "poQty": qty})
	return flags


# --- reading it back --------------------------------------------------------------------------


def _file_info(row) -> tuple[str | None, str | None]:
	file_name = row.po_file if row and row.po_file and frappe.db.exists("File", row.po_file) else None
	return file_name, (row.file_name if file_name else None)


def po_state(inquiry: str, for_staff: bool = False) -> dict:
	"""The PO box of one inquiry. The dealer's view carries only whether a PO is on file and under what
	name (plus whether one is required); the status, the check result and every staff stamp are
	staff-only and never part of it."""
	dpo = latest_for("inquiry", inquiry)
	if for_staff and dpo:
		return dpo_state(dpo)
	row = frappe.db.get_value(DPO, dpo, ["po_file", "file_name"], as_dict=True) if dpo else None
	_file, name = _file_info(row)
	return {"required": po_required(), "attached": bool(_file), "fileName": name}


def dpo_state(dpo: str) -> dict:
	"""Everything the staff app shows for one Dealer Purchase Order."""
	row = frappe.db.get_value(
		DPO,
		dpo,
		["dealer", "dealer_name", "customer_po_number", "po_date", "inquiry", "quotation", "sales_order", "po_file", "file_name", "status",
		 "check_result", "attached_by", "attached_on", "reviewed_by", "reviewed_on", "flag_count", "check_summary"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Dealer Purchase Order {0} not found.").format(dpo), frappe.DoesNotExistError)
	file_name, display = _file_info(row)
	result = None
	if row.check_result:
		try:
			result = json.loads(row.check_result)
		except ValueError:
			result = None
	return {
		"id": dpo,
		"dealer": row.dealer,
		"dealerName": row.dealer_name,
		"customerPoNumber": row.customer_po_number,
		"poDate": str(row.po_date) if row.po_date else None,
		"inquiry": row.inquiry,
		"quotation": row.quotation,
		"salesOrder": row.sales_order,
		"required": po_required(),
		"attached": bool(file_name),
		"fileName": display,
		"status": row.status if file_name else None,
		"attachedBy": row.attached_by,
		"attachedOn": str(row.attached_on) if row.attached_on else None,
		"reviewedBy": row.reviewed_by,
		"reviewedOn": str(row.reviewed_on) if row.reviewed_on else None,
		"flagCount": row.flag_count or 0,
		"summary": row.check_summary,
		"result": result if file_name else None,
	}


def _no_po(required: bool | None = None) -> dict:
	return {
		"id": None, "dealer": None, "dealerName": None, "customerPoNumber": None, "poDate": None,
		"inquiry": None, "quotation": None, "salesOrder": None,
		"required": po_required() if required is None else required, "attached": False, "fileName": None, "status": None,
		"attachedBy": None, "attachedOn": None, "reviewedBy": None, "reviewedOn": None, "flagCount": 0, "summary": None, "result": None,
	}


def po_file_payload(dpo: str) -> dict:
	file_name = frappe.db.get_value(DPO, dpo, "po_file")
	if not file_name or not frappe.db.exists("File", file_name):
		frappe.throw(_("No file is attached to {0}.").format(dpo), frappe.DoesNotExistError)
	content = _file_bytes(file_name)
	return {"fileName": frappe.db.get_value(DPO, dpo, "file_name"), "mime": sniff_mime(content), "data": base64.b64encode(content).decode("ascii")}


# --- staff endpoints --------------------------------------------------------------------------
# The dealer's own two (attach_po, get_my_po) live in dealer_portal_api: that module's path prefix
# is the only one a dealer-only session may call (auth.middleware.DEALER_PORTAL_PREFIXES).


def _assert_can_write() -> None:
	from dms_erp.sales.inquiry_api import _assert_can_manage_inquiries

	_assert_can_manage_inquiries()


def _assert_can_read(dpo: str) -> None:
	if not frappe.has_permission(DPO, "read", dpo):
		frappe.throw(_("You do not have access to Dealer Purchase Order {0}.").format(dpo), frappe.PermissionError)


def _uploaded_file():
	if "file" not in frappe.request.files:
		frappe.throw(_("No file uploaded."), frappe.ValidationError)
	uploaded = frappe.request.files["file"]
	# One byte past the limit is read, so an oversized upload is refused without loading all of it.
	return uploaded.filename, uploaded.stream.read(MAX_PO_BYTES + 1)


@frappe.whitelist(methods=["GET"])
def list_dealer_pos(
	search: str | None = None,
	status: str | None = None,
	dealer: str | None = None,
	limit: int = 25,
	offset: int = 0,
):
	"""Every Dealer Purchase Order, newest first. `search` matches the dealer's name, the PO number, the
	file name and the linked inquiry / quotation / order ids."""
	from dms_erp.pagination import clamp

	if not frappe.has_permission(DPO, "read"):
		frappe.throw(_("You do not have access to Dealer Purchase Orders."), frappe.PermissionError)
	limit, offset = clamp(limit, offset)
	filters = {}
	if status:
		filters["status"] = status
	if dealer:
		filters["dealer"] = dealer
	or_filters = None
	if search and search.strip():
		like = f"%{search.strip()}%"
		or_filters = [[f, "like", like] for f in ("dealer_name", "customer_po_number", "file_name", "inquiry", "quotation", "sales_order", "name")]
	total = len(frappe.get_all(DPO, filters=filters, or_filters=or_filters, pluck="name"))
	rows = frappe.get_all(
		DPO,
		filters=filters,
		or_filters=or_filters,
		fields=["name", "dealer", "dealer_name", "customer_po_number", "po_date", "file_name", "status", "inquiry", "quotation", "sales_order", "attached_on", "flag_count"],
		order_by="creation desc",
		limit_start=offset,
		limit_page_length=limit,
	)
	items = [
		{
			"id": r.name, "dealer": r.dealer, "dealerName": r.dealer_name, "customerPoNumber": r.customer_po_number,
			"poDate": str(r.po_date) if r.po_date else None, "fileName": r.file_name, "status": r.status,
			"inquiry": r.inquiry, "quotation": r.quotation, "salesOrder": r.sales_order,
			"attachedOn": str(r.attached_on) if r.attached_on else None, "flagCount": r.flag_count or 0,
		}
		for r in rows
	]
	return {"items": items, "total": total, "limit": limit, "offset": offset}


@frappe.whitelist(methods=["GET"])
def get_dealer_po(name: str):
	_assert_can_read(name)
	return dpo_state(name)


@frappe.whitelist(methods=["GET"])
def get_dealer_po_for(doctype: str, name: str):
	"""The PO of an Inquiry, Quotation or Sales Order -- what those documents' panels show. An empty box
	(attached False) when there is none yet, so the panel can offer to attach one."""
	field = {"Inquiry": "inquiry", "Quotation": "quotation", "Sales Order": "sales_order"}.get(doctype)
	if not field:
		frappe.throw(_("A PO is linked to an Inquiry, Quotation or Sales Order."), frappe.ValidationError)
	dpo = latest_for(field, name)
	if not dpo and doctype == "Quotation":
		dpo = latest_for("inquiry", frappe.db.get_value("Quotation", name, "custom_inquiry"))
	if not dpo and doctype == "Sales Order":
		source = frappe.db.get_value("Sales Order", name, ["custom_source_type", "custom_source_ref"], as_dict=True)
		if source and source.custom_source_type == "Inquiry":
			dpo = latest_for("inquiry", source.custom_source_ref)
		elif source and source.custom_source_type == "Quotation":
			dpo = latest_for("quotation", source.custom_source_ref)
	if not dpo:
		return _no_po()
	_assert_can_read(dpo)
	return dpo_state(dpo)


@frappe.whitelist(methods=["POST"])
def create_dealer_po(dealer: str | None = None, inquiry: str | None = None, quotation: str | None = None, sales_order: str | None = None, customer_po_number: str | None = None):
	"""Staff add a dealer's PO -- multipart, form field "file". Link it to the inquiry / quotation /
	order it is for (the dealer defaults to theirs), or leave it unlinked and link it later."""
	_assert_can_write()
	filename, content = _uploaded_file()
	dpo = create_po(dealer, filename, content, inquiry=inquiry, quotation=quotation, sales_order=sales_order, customer_po_number=customer_po_number)
	return dpo_state(dpo)


@frappe.whitelist(methods=["POST"])
def replace_dealer_po_file(name: str):
	_assert_can_write()
	filename, content = _uploaded_file()
	replace_file(name, filename, content)
	return dpo_state(name)


@frappe.whitelist(methods=["GET"])
def get_dealer_po_file(name: str):
	"""The PO's bytes (base64) for the staff app to show. Private files cannot be opened with the
	app's JWT directly, so this role-checked read is the way in."""
	_assert_can_read(name)
	return po_file_payload(name)


@frappe.whitelist(methods=["POST"])
def update_dealer_po(name: str, patch: str | dict):
	"""Staff correct a PO's links and number. `patch` keys: inquiry, quotation, sales_order,
	customer_po_number, po_date -- an empty value clears it. Every link must be the PO's own dealer's
	document (the doctype checks). Linking an order also links the PO's file onto it."""
	_assert_can_write()
	patch = frappe.parse_json(patch) if isinstance(patch, str) else patch
	allowed = {"inquiry", "quotation", "sales_order", "customer_po_number", "po_date"}
	unknown = set(patch) - allowed
	if unknown:
		frappe.throw(_("Cannot change: {0}").format(", ".join(sorted(unknown))), frappe.ValidationError)
	doc = frappe.get_doc(DPO, name)
	for key, value in patch.items():
		doc.set(key, (value or None) if isinstance(value, str) or value is None else value)
	doc.save(ignore_permissions=True)
	if doc.sales_order and doc.po_file:
		_link_file_to("Sales Order", doc.sales_order, doc.po_file)
	return dpo_state(name)


@frappe.whitelist(methods=["POST"])
def recheck_dealer_po(name: str):
	"""Run the automatic check again (a better model, a fixed key, a re-read)."""
	_assert_can_write()
	file_name = frappe.db.get_value(DPO, name, "po_file")
	if not file_name:
		frappe.throw(_("No file is attached to {0}.").format(name), frappe.ValidationError)
	frappe.db.set_value(
		DPO, name,
		{"status": "Attached", "check_result": None, "check_summary": None, "flag_count": 0, "reviewed_by": None, "reviewed_on": None},
		update_modified=False,
	)
	_queue_check(name, file_name)
	return dpo_state(name)


@frappe.whitelist(methods=["POST"])
def review_dealer_po(name: str):
	"""A person has looked at the PO against what it is linked to and any flags: stamps who and when.
	The check's own result stays as it was, so what was flagged is still on record."""
	_assert_can_write()
	if not frappe.db.get_value(DPO, name, "po_file"):
		frappe.throw(_("No file is attached to {0}.").format(name), frappe.ValidationError)
	frappe.db.set_value(DPO, name, {"status": "Reviewed", "reviewed_by": frappe.session.user, "reviewed_on": now_datetime()}, update_modified=False)
	return dpo_state(name)
