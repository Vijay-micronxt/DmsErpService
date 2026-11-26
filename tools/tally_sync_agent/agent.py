"""DMS -> Tally sync agent. Runs on the PC where TallyPrime runs (or anywhere on the
same network), because the cloud ERP cannot reach Tally and Tally cannot push.

Loop: ask the ERP for submitted Sales Invoices ticked "Sync to Tally"
(dms_erp.sales.tally_sync.get_pending_invoices -- the ERP has already turned each
one into a Tally Sales voucher), skip any whose voucher number Tally already has
(so a retry after a lost reply can never create a duplicate), create the rest
through Tally's XML port, and report each outcome back to the ERP.

Standard library only, so the Tally PC needs nothing but Python 3.10+.

    python agent.py --config config.ini          # keep running
    python agent.py --config config.ini --once   # one pass, then exit
    python agent.py --config config.ini --vouchers [--dry-run]   # import the configured window of Tally vouchers, once
    python agent.py --config config.ini --schedule               # ... and again at the times in [schedule]
"""

import argparse
import configparser
import datetime
import html
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

log = logging.getLogger("tally_sync_agent")


class Config:
	def __init__(self, path):
		cp = configparser.ConfigParser(interpolation=None)
		if not cp.read(path, encoding="utf-8"):
			sys.exit(f"Config file not found: {path}")
		self.erp_url = cp["erp"]["url"].rstrip("/")
		self.api_key = cp["erp"]["api_key"]
		self.api_secret = cp["erp"]["api_secret"]
		self.tally_url = cp["tally"]["url"].rstrip("/")
		# Exactly as Tally shows it, spaces included (e.g. "Pacific Inc  -  (From 1-Apr-2025)").
		self.company = cp["tally"]["company"]
		# Unlicensed (Educational mode) TallyPrime only accepts the 1st, 2nd and 31st of a
		# month -- for testing against one, move each voucher to the 1st of its month.
		# Never set this against a licensed Tally.
		self.educational_mode = cp.getboolean("tally", "educational_mode", fallback=False)
		self.interval = int(cp.get("agent", "interval_seconds", fallback="60"))
		self.batch_size = int(cp.get("agent", "batch_size", fallback="20"))
		# [vouchers]: which Tally vouchers to import and from when; [schedule]: when the agent runs by itself.
		self.voucher_days_back = int(cp.get("vouchers", "from_days_back")) if cp.get("vouchers", "from_days_back", fallback="").strip() else None
		self.voucher_from_date = _yyyymmdd(cp.get("vouchers", "from_date", fallback="")) if cp.get("vouchers", "from_date", fallback="").strip() else None
		self.voucher_types = _canon_types(cp.get("vouchers", "types", fallback=""))
		# Vouchers read from Tally per request. Tally spends about the same time on a request however few
		# it returns (it scans the whole year to find them), so reading in batches is much faster.
		self.read_batch = max(1, int(cp.get("vouchers", "read_batch", fallback="25")))
		self.schedule = parse_schedule({k: v for k, v in (cp.items("schedule") if cp.has_section("schedule") else [])})


# ------------------------------------------------------------------------- ERP


def erp_call(cfg, method, http="GET", data=None):
	url = f"{cfg.erp_url}/api/method/{method}"
	body = None
	if http == "GET" and data:
		url += "?" + urllib.parse.urlencode(data)
	elif data is not None:
		body = urllib.parse.urlencode(data).encode()
	req = urllib.request.Request(url, data=body, method=http)
	req.add_header("Authorization", f"token {cfg.api_key}:{cfg.api_secret}")
	req.add_header("Accept", "application/json")
	with urllib.request.urlopen(req, timeout=60) as r:
		return json.loads(r.read().decode("utf-8")).get("message")


# ----------------------------------------------------------------------- Tally


def tally_post(cfg, xml):
	req = urllib.request.Request(cfg.tally_url, data=xml.encode("utf-8"), method="POST")
	req.add_header("Content-Type", "text/xml; charset=utf-8")
	with urllib.request.urlopen(req, timeout=120) as r:
		raw = r.read()
	# Tally answers in UTF-16 or UTF-8 depending on version/settings.
	if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
		return raw.decode("utf-16")
	try:
		return raw.decode("utf-8")
	except UnicodeDecodeError:
		# An item like "Decor" with an accented e arrives as a single Windows-1252 byte; decoding it as
		# UTF-8 with "replace" turned it into a "?" and the item could not be found in the ERP.
		return raw.decode("cp1252", errors="replace")


def tally_find_voucher(cfg, voucher_type, reference):
	"""(voucher number, MASTERID) of the voucher carrying this ERP invoice number as its
	Reference, else None. Matched on Reference, not voucher number: Pacific's Sales
	voucher type numbers itself (PI/NNNNN/26-27) and ignores the number we send."""
	formula = f'$Reference = "{reference}"'
	xml = (
		"<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>"
		"<ID>DmsFindVoucher</ID></HEADER><BODY><DESC><STATICVARIABLES>"
		"<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
		f"<SVCURRENTCOMPANY>{escape(cfg.company)}</SVCURRENTCOMPANY>"
		"</STATICVARIABLES><TDL><TDLMESSAGE>"
		# vouchers of this type only (Tally walks just that type's list -- about half the
		# time of filtering every voucher in the company)
		'<COLLECTION NAME="DmsFindVoucher"><TYPE>Vouchers : VoucherType</TYPE>'
		f"<CHILDOF>{escape(voucher_type)}</CHILDOF><BELONGSTO>Yes</BELONGSTO>"
		"<FETCH>VoucherNumber, MasterId</FETCH>"
		"<FILTER>DmsVoucherMatch</FILTER></COLLECTION>"
		f'<SYSTEM TYPE="Formulae" NAME="DmsVoucherMatch">{escape(formula)}</SYSTEM>'
		"</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
	)
	out = tally_post(cfg, xml)
	m = re.search(r"<VOUCHER [^>]*>(.*?)</VOUCHER>", out, re.DOTALL)
	if not m:
		return None
	number = re.search(r"<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>", m.group(1), re.DOTALL)
	master = re.search(r"<MASTERID[^>]*>\s*(\d+)\s*</MASTERID>", m.group(1))
	return (html.unescape(number.group(1).strip()) if number else ""), (master.group(1) if master else "")


def tally_create_voucher(cfg, voucher_xml):
	"""(ok, error_text). Tally reports per-request counters plus LINEERROR text."""
	xml = (
		"<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
		"<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME><STATICVARIABLES>"
		f"<SVCURRENTCOMPANY>{escape(cfg.company)}</SVCURRENTCOMPANY>"
		"</STATICVARIABLES></REQUESTDESC><REQUESTDATA>"
		f'<TALLYMESSAGE xmlns:UDF="TallyUDF">{voucher_xml}</TALLYMESSAGE>'
		"</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
	)
	out = tally_post(cfg, xml)

	def count(tag):
		m = re.search(rf"<{tag}>\s*(-?\d+)\s*</{tag}>", out)
		return int(m.group(1)) if m else 0

	errors = [html.unescape(e).strip() for e in re.findall(r"<LINEERROR>(.*?)</LINEERROR>", out, re.DOTALL)]
	if count("CREATED") >= 1 and not count("ERRORS") and not count("EXCEPTIONS"):
		return True, None
	return False, "; ".join(errors) or f"Tally did not create the voucher: {out.strip()[:500]}"


# ------------------------------------------------------------------------ loop


def _first_of_month(voucher_xml, date):
	first = date[:6] + "01"
	return (
		voucher_xml.replace(f"<DATE>{date}</DATE>", f"<DATE>{first}</DATE>")
		.replace(f"<EFFECTIVEDATE>{date}</EFFECTIVEDATE>", f"<EFFECTIVEDATE>{first}</EFFECTIVEDATE>")
		.replace("</NARRATION>", f" (real date {date[6:]}-{date[4:6]}-{date[:4]}, moved for Educational-mode Tally)</NARRATION>")
	)


def sync_once(cfg):
	pending = erp_call(
		cfg, "dms_erp.sales.tally_sync.get_pending_invoices", http="POST", data={"limit": cfg.batch_size}
	) or []
	for inv in pending:
		name = inv["invoice"]
		voucher_xml = inv["voucher_xml"]
		if cfg.educational_mode:
			voucher_xml = _first_of_month(voucher_xml, inv["date"])
		try:
			existing = tally_find_voucher(cfg, inv["voucher_type"], inv["reference"])
			if existing:
				ok, err = True, None
				log.info("%s already in Tally as %s -- marking synced", name, existing[0])
			else:
				ok, err = tally_create_voucher(cfg, voucher_xml)
				existing = tally_find_voucher(cfg, inv["voucher_type"], inv["reference"]) if ok else None
				log.info(
					"%s -> Tally: %s%s", name, f"created {existing[0]}" if ok and existing else "created" if ok else "FAILED", f" ({err})" if err else ""
				)
		except (urllib.error.URLError, OSError) as e:
			# Tally closed / company not open: leave it Pending, try again next pass.
			log.warning("Tally not reachable for %s: %s -- will retry", name, e)
			return
		erp_call(
			cfg,
			"dms_erp.sales.tally_sync.report_sync_result",
			http="POST",
			data={"invoice": name, "ok": 1 if ok else 0, "tally_voucher_no": (existing or ("", ""))[0], "error": err or ""},
		)
	if pending:
		log.info("pass done: %d invoice(s)", len(pending))


def _tag(block, *names):
	for name in names:
		m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.DOTALL)
		if m and m.group(1).strip():
			return html.unescape(m.group(1).strip())
	return ""


def _tally_export(cfg, kind, rid, static="", tdl=""):
	return tally_post(
		cfg,
		f"<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>{kind}</TYPE><ID>{rid}</ID></HEADER>"
		"<BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
		f"<SVCURRENTCOMPANY>{escape(cfg.company)}</SVCURRENTCOMPANY>{static}</STATICVARIABLES>{tdl}</DESC></BODY></ENVELOPE>",
	)


VOUCHER_TYPES = (
	"Sales",
	"Delivery Note",
	"Purchase",
	"Purchase (New)",
	"Purchase Transit",
	"Bank Receipt",
	"Cash Receipt",
	"Bank Payment",
	"Cash Payment",
	"Payment",
	"Contra",
	"Credit Note",
	"Credit Note ( Purchase)",  # Tally spells it with the space
	"Debit Note",
	"Debit Note (Sales)",
)


def _control_free(text):
	"""Tally sends "&#4; Not Applicable" for an empty tracking slot; keep "Not Applicable"."""
	return re.sub(r"[\x00-\x1f]", "", text or "").strip()


def _voucher_collection(cfg, fetch, where=""):
	filters = "<FILTERS>Pick</FILTERS>" if where else ""
	formula = f'<SYSTEM TYPE="Formulae" NAME="Pick">{where}</SYSTEM>' if where else ""
	return _tally_export(
		cfg,
		"Collection",
		"VchRead",
		tdl=(
			'<TDL><TDLMESSAGE><COLLECTION NAME="VchRead" ISMODIFY="No"><TYPE>Voucher</TYPE>'
			f"{filters}<FETCH>{fetch}</FETCH></COLLECTION>{formula}</TDLMESSAGE></TDL>"
		),
	)


def _yyyymmdd(text):
	"""A date as 2026-08-15, 15-08-2026 or 20260815 -> "20260815"; exits with a clear message if it is none of them."""
	t = text.strip()
	for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y%m%d"):
		try:
			return datetime.datetime.strptime(t, fmt).strftime("%Y%m%d")
		except ValueError:
			pass
	sys.exit(f"config.ini: '{text}' is not a date (use YYYY-MM-DD).")


def _canon_types(text):
	"""The voucher types in config as Tally spells them (it spells one "Credit Note ( Purchase)", with a
	stray space), or None for all of them. An unknown type stops the agent rather than being ignored."""
	names = [t.strip() for t in text.split(",") if t.strip()]
	if not names:
		return None
	squash = lambda x: re.sub(r"\s+", "", x).lower()
	out = []
	for name in names:
		match = next((t for t in VOUCHER_TYPES if squash(t) == squash(name)), None)
		if not match:
			sys.exit(f"config.ini [vouchers] types: '{name}' is not one of: {', '.join(VOUCHER_TYPES)}")
		out.append(match)
	return out


def since_formula(since):
	"""Tally formula: the voucher is dated `since` (YYYYMMDD) or later. Found by testing against Tally:
	the from/to dates sent with a request are ignored (it answers for the whole financial year), and a
	two-sided formula or arithmetic on the date is refused, but this one-sided comparison of year, then
	month, then day is applied and was exact for every start date tried. Tally still checks every
	voucher (about as long as asking for them all), so what it saves is the reply, not the scan."""
	y, m, d = int(since[:4]), int(since[4:6]), int(since[6:8])
	Y, M, D = "$$YearOfDate:$Date", "$$MonthOfDate:$Date", "$$DayOfDate:$Date"
	return f"({Y} > {y}) OR ({Y} = {y} AND {M} > {m}) OR ({Y} = {y} AND {M} = {m} AND {D} >= {d})"


def resolve_since(cfg, today=None):
	"""The first day of the window: config's fixed from_date, else today minus from_days_back, else None (the whole year)."""
	if cfg.voucher_from_date:
		return cfg.voucher_from_date
	if cfg.voucher_days_back is not None:
		return ((today or datetime.date.today()) - datetime.timedelta(days=cfg.voucher_days_back)).strftime("%Y%m%d")
	return None


def tally_voucher_index(cfg, since=None):
	"""[{guid, type, number, date, cancelled, optional}] for the company's vouchers dated `since` or later
	(the whole current financial year when none) -- ids and headings only, the cheap way to find what is
	new (about 13 s for 12,000 vouchers). Never asks for the lines of many vouchers at once: that stalls Tally."""
	xml = _voucher_collection(cfg, "DATE,VOUCHERTYPENAME,VOUCHERNUMBER,GUID,ALTERID,ISCANCELLED,ISOPTIONAL", where=since_formula(since) if since else "")
	out = []
	for block in re.findall(r"<VOUCHER .*?</VOUCHER>", xml, re.DOTALL):
		guid = _tag(block, "GUID")
		if guid:
			out.append(
				{
					"guid": guid,
					"type": _tag(block, "VOUCHERTYPENAME"),
					"number": _tag(block, "VOUCHERNUMBER"),
					"date": re.sub(r"\D", "", _tag(block, "DATE")),
					"cancelled": _tag(block, "ISCANCELLED") == "Yes",
					"optional": _tag(block, "ISOPTIONAL") == "Yes",
				}
			)
	return out


# Pacific's transport details are custom fields in their Tally, so Tally names them by number
# ("UDF:_UDF_788538166" is the transporter). These ids belong to THIS company's Tally: another
# Tally company has other numbers, so they are read from here, not hard-wired into the ERP.
TRANSPORT_UDFS = {
	"transporter": "788538166",
	"lr_no": "788531187",
	"lr_date": "754976756",
	"driver_name": "788538167",
	"driver_phone": "788538168",
	"driver_licence": "788538174",
	"vehicle_owner": "788531216",
	"vehicle_owner_pan": "788531217",
	"transporter_pan": "788538175",
	"transporter_gstin": "788538177",
	"vehicle_permit": "788538176",
}


def _exact(block, *names):
	"""The text of the first non-empty <NAME ...>text</NAME>, matching that tag name only. _tag() allows
	anything after the name, so _tag("REFERENCE") also matched <REFERENCEDATE> and ran on through the
	XML -- harmless with a short field list, wrong once a voucher is fetched in full."""
	for name in names:
		m = re.search(rf"<{re.escape(name)}(?:\s[^>]*)?>(.*?)</{re.escape(name)}>", block, re.DOTALL)
		if m and m.group(1).strip():
			return html.unescape(m.group(1).strip())
	return ""


# Pacific's custom fields for who entered / approved a voucher and every alteration of it, and the
# custom item fields. Like TRANSPORT_UDFS the numbers belong to THIS company's Tally; the names are the
# ones the migration stored them under in the ERP's `tally_udf` fields.
AUDIT_UDFS = {
	"entered_by": "788564185",
	"entered_on": "788564186",
	"approved_by": "788564183",
	"approved_on": "788564184",
	"classification": "788531255",
	"prev_log": "671096245",
}
LOG_GROUP_UDF = "805313969"  # one group per alteration: log no, user, reason, date
LOG_UDFS = {"log_no": "671096242", "user": "788536755", "reason": "788536756", "altered_on": "788536758"}
LINE_UDFS = {
	# Tally keeps a line's total and each batch's own share under different ids: the line (all batches) ...
	"PIITEMSQMTR": "671097642",
	"PIITEMWEIGHT": "671097653",
	"PIITEMUPDATED": "754983739",
	"PIITEMTYPE": "788538153",
	# ... and the batch allocation (equal to the line's on a single-batch line)
	"PIITEMBTACHSQMTR": "671097650",
	"PIITEMQTYVAL": "671097651",
	"PIITEMBATCHWEIGHT": "671097661",
	"PIBATCHITEMTYPE": "788538161",
	"PIVCHRESERVEDSTATUS": "788538164",
}


def _udf(text, uid):
	return _exact(text, f"UDF:_UDF_{uid}")


def _voucher_top(block):
	"""The voucher with its stock and ledger lines taken out: what is left is the voucher's own fields."""
	return re.sub(r"<(ALLINVENTORYENTRIES|LEDGERENTRIES|ALLLEDGERENTRIES)\.LIST>.*?</\1\.LIST>", "", block, flags=re.DOTALL)


def _audit(block):
	"""Who entered and approved the voucher and when, every alteration with its reason, and the custom
	values under the names the ERP keeps them by. Blank ones are left out."""
	groups = re.findall(rf"<UDF:_UDF_{LOG_GROUP_UDF}\.LIST[^>]*>.*?</UDF:_UDF_{LOG_GROUP_UDF}\.LIST>", block, re.DOTALL)
	logs = [{k: _udf(g, uid) for k, uid in LOG_UDFS.items()} for g in groups]
	top = _voucher_top(block)
	top = re.sub(rf"<UDF:_UDF_{LOG_GROUP_UDF}\.LIST[^>]*>.*?</UDF:_UDF_{LOG_GROUP_UDF}\.LIST>", "", top, flags=re.DOTALL)
	a = {k: _udf(top, uid) for k, uid in AUDIT_UDFS.items()}
	udf = {}
	if logs:
		last = logs[-1]
		udf["BESALTERATIONHISTORYLOGS"] = f"{last['log_no']} | {last['user']} | {last['altered_on']}"
	for name, value in (
		("BESALTERATIONHISTORYPREVLOG", a["prev_log"]),
		("PINCSALESVCHCUSTOMERCLASSIFICATIONS", a["classification"]),
		("BESPLVCHENTEREDBY", a["entered_by"]),
		("BESPLVCHENTEREDDATETIME", a["entered_on"]),
		("BESPLVCHAPPROVEDBY", a["approved_by"]),
		("BESPLVCHAPPROVEDON", a["approved_on"]),
	):
		if value:
			udf[name] = value
	return {
		"entered_by": a["entered_by"],
		"entered_on": a["entered_on"],
		"approved_by": a["approved_by"],
		"approved_on": a["approved_on"],
		"classification": a["classification"],
		"alterations": logs,
		"udf": udf,
	}


def _transport(block):
	"""The invoice's e-invoice and transport details: IRN and its acknowledgement, e-way bill, vehicle,
	who entered it, and Pacific's own transporter / LR / driver fields. Blank ones are left out."""
	eway = re.search(r"<EWAYBILLDETAILS\.LIST>.*?</EWAYBILLDETAILS\.LIST>", block, re.DOTALL)
	found = {
		"vehicle": _exact(block, "VEHICLENUMBER") or _exact(block, "BASICSHIPVESSELNO"),
		"eway_bill": _exact(eway.group(0), "BILLNUMBER") if eway else "",
		"irn": _exact(block, "IRN"),
		"irn_ack_no": _exact(block, "IRNACKNO"),
		"irn_ack_date": _exact(block, "IRNACKDATE"),
		"entered_by": _exact(block, "ENTEREDBY"),
	}
	for key, udf in TRANSPORT_UDFS.items():
		found[key] = _exact(block, f"UDF:_UDF_{udf}")
	return {k: v for k, v in found.items() if v}


def _ledger_lines(block):
	"""[{ledger, amount, party, bills, instrument, instrument_date}] for a voucher. An invoice keeps its
	ledgers under LEDGERENTRIES.LIST; a receipt, payment or contra under ALLLEDGERENTRIES.LIST -- the
	second is used when present, so a voucher is never counted twice. `bills` are the invoices a receipt or
	payment settles ("Agst Ref"); `instrument` is the cheque / transfer reference printed on the bank line."""
	lines = re.findall(r"<ALLLEDGERENTRIES\.LIST>.*?</ALLLEDGERENTRIES\.LIST>", block, re.DOTALL) or re.findall(
		r"<LEDGERENTRIES\.LIST>.*?</LEDGERENTRIES\.LIST>", block, re.DOTALL
	)
	out = []
	for line in lines:
		bills = [
			{"name": _exact(b, "NAME"), "type": _exact(b, "BILLTYPE"), "amount": _exact(b, "AMOUNT")}
			for b in re.findall(r"<BILLALLOCATIONS\.LIST>.*?</BILLALLOCATIONS\.LIST>", line, re.DOTALL)
			if _exact(b, "NAME")
		]
		bank = re.search(r"<BANKALLOCATIONS\.LIST>.*?</BANKALLOCATIONS\.LIST>", line, re.DOTALL)
		out.append(
			{
				"ledger": _exact(line, "LEDGERNAME"),
				"amount": _exact(line, "AMOUNT"),
				"party": _exact(line, "ISPARTYLEDGER") == "Yes",
				"bills": bills,
				"instrument": _exact(bank.group(0), "INSTRUMENTNUMBER") if bank else "",
				"instrument_date": re.sub(r"\D", "", _exact(bank.group(0), "INSTRUMENTDATE")) if bank else "",
				"unique_ref": _exact(bank.group(0), "UNIQUEREFERENCENUMBER") if bank else "",
			}
		)
	return out


# "*" is every plain field (and Pacific's custom ones) but NOT the stock and ledger lines: those are
# sub-collections Tally only returns when asked for by name.
FULL_FETCH = "*,ALLINVENTORYENTRIES.*,LEDGERENTRIES.*,ALLLEDGERENTRIES.*"


def tally_voucher_details(cfg, guids):
	"""{guid: voucher in full} for several vouchers in ONE request. Tally scans the whole year to find a
	voucher by id (about 8 s on 12,000), whether it is asked for one or for five, so five in a request took
	9 s against 12 s for one alone; ask for a few dozen at a time, not thousands (that stalls it)."""
	out = {}
	if not guids:
		return out
	xml = _voucher_collection(cfg, FULL_FETCH, where=" OR ".join(f'$GUID = "{g}"' for g in guids))
	for block in re.findall(r"<VOUCHER .*?</VOUCHER>", xml, re.DOTALL):
		d = _voucher_from_block(block)
		if d["guid"]:
			out[d["guid"]] = d
	return out


def tally_voucher_detail(cfg, guid):
	"""One voucher in full, or None."""
	return tally_voucher_details(cfg, [guid]).get(guid)


def _voucher_from_block(block):
	"""One voucher in full: its stock lines [{item, godown, batch, quantity, amount, tracking}], one
	per batch allocation, its tax ledgers, transport and audit details."""
	rows = []
	for line in re.findall(r"<ALLINVENTORYENTRIES\.LIST>.*?</ALLINVENTORYENTRIES\.LIST>", block, re.DOTALL):
		item = _exact(line, "STOCKITEMNAME")
		if not item:
			continue  # a ledger-only line
		allocations = re.findall(r"<BATCHALLOCATIONS\.LIST>.*?</BATCHALLOCATIONS\.LIST>", line, re.DOTALL) or [line]
		line_top = re.sub(r"<BATCHALLOCATIONS\.LIST>.*?</BATCHALLOCATIONS\.LIST>", "", line, flags=re.DOTALL)
		for alloc in allocations:
			udf = {name: v for name, uid in LINE_UDFS.items() if (v := _udf(alloc, uid) or _udf(line_top, uid))}
			rows.append(
				{
					"udf": udf,
					"rate": _exact(line_top, "RATE"),
					"discount": _exact(line_top, "DISCOUNT"),
					"item": item,
					"godown": _exact(alloc, "GODOWNNAME"),
					"batch": _exact(alloc, "BATCHNAME"),
					"quantity": _exact(alloc, "ACTUALQTY", "BILLEDQTY"),
					"amount": _exact(alloc, "AMOUNT"),
					"tracking": _control_free(_exact(alloc, "TRACKINGNUMBER")),
					# The sales / purchase ledger this line is booked to (inside the item line, not the batch).
					"account": _exact(line, "LEDGERNAME"),
					"hsn": _exact(line, "GSTHSNNAME"),
				}
			)
	return {
		"guid": _exact(block, "GUID"),
		"type": _exact(block, "VOUCHERTYPENAME"),
		"number": _exact(block, "VOUCHERNUMBER"),
		"date": re.sub(r"\D", "", _exact(block, "DATE")),
		"party": _exact(block, "PARTYLEDGERNAME"),
		"party_gstin": _exact(block, "PARTYGSTIN"),
		# Taxes, round-off, freight (invoices) or the money and the party (receipts, payments, contra):
		# every ledger line, with Tally's sign (negative = debit).
		"ledgers": _ledger_lines(block),
		"transport": _transport(block),
		"audit": _audit(block),
		"reference_date": re.sub(r"\D", "", _exact(_voucher_top(block), "REFERENCEDATE")),
		"reference": _exact(block, "REFERENCE"),
		"narration": _exact(block, "NARRATION"),
		"cancelled": _exact(block, "ISCANCELLED") == "Yes",
		"optional": _exact(block, "ISOPTIONAL") == "Yes",
		"rows": rows,
	}


def tally_item_groups(cfg):
	"""{stock item name: its stock group} -- a new item bought from a supplier is filed under the same
	group in the ERP, as the migration did. One small read of the item list (no vouchers)."""
	xml = _tally_export(
		cfg,
		"Collection",
		"ItemGroups",
		tdl='<TDL><TDLMESSAGE><COLLECTION NAME="ItemGroups" ISMODIFY="No"><TYPE>StockItem</TYPE><FETCH>NAME,PARENT</FETCH></COLLECTION></TDLMESSAGE></TDL>',
	)
	groups = {}
	for block in re.findall(r"<STOCKITEM .*?</STOCKITEM>", xml, re.DOTALL):
		name = html.unescape(re.search(r'NAME="([^"]*)"', block).group(1)) if re.search(r'NAME="([^"]*)"', block) else _tag(block, "NAME")
		parent = _tag(block, "PARENT")
		if name and parent:
			groups[name] = parent
	return groups


def send_vouchers(cfg, dry_run=False, since=None, limit=None, types=None):
	index = tally_voucher_index(cfg, since)
	by_type = {}
	for v in index:
		by_type[v["type"]] = by_type.get(v["type"], 0) + 1
	log.info(
		"Tally has %d voucher(s) %s: %s",
		len(index),
		f"dated {since} or later" if since else "this year",
		", ".join(f"{t} {n}" for t, n in sorted(by_type.items(), key=lambda x: -x[1])[:8]),
	)
	kinds = tuple(types) if types else VOUCHER_TYPES
	of_kind = [v for v in index if v["type"] in kinds and (not since or v["date"] >= since)]
	# Tally already says in the headings which are cancelled or optional: they are never imported, so
	# they are not read in full (they are never in the ERP, so they would be read again on every run).
	wanted = [v for v in of_kind if not (v["cancelled"] or v["optional"])]
	log.info("%d of them are %s; %d cancelled or optional left alone", len(wanted), "/".join(kinds), len(of_kind) - len(wanted))
	if not wanted:
		return
	guids = [v["guid"] for v in wanted]
	new = set()
	for start in range(0, len(guids), 3000):
		chunk = guids[start : start + 3000]
		new.update(erp_call(cfg, "dms_erp.warehouse.tally_vouchers.filter_new_tally_vouchers", http="POST", data={"guids": json.dumps(chunk)}) or [])
	fresh = sorted((v for v in wanted if v["guid"] in new), key=lambda v: (v["date"], v["number"]))
	log.info("%d are not in the ERP yet", len(fresh))
	if limit:
		fresh = fresh[:limit]
	if not fresh:
		return
	if dry_run:
		for v in fresh[:20]:
			log.info("  new: %s | %s | %s", v["date"], v["type"], v["number"])
		details = tally_voucher_details(cfg, [v["guid"] for v in fresh[:3]])
		for v in fresh[:3]:
			d = details.get(v["guid"])
			log.info("  %s %s: %d stock line(s), e.g. %s", v["type"], v["number"], len(d["rows"]) if d else 0, d["rows"][0] if d and d["rows"] else "-")
		log.info("dry run: nothing sent to the ERP")
		return
	made, unplaced, ignored, already = 0, [], {}, 0
	new_items, new_batches = [], []
	# Only a purchase can bring an item the ERP lacks, and only then is the stock group needed.
	groups = tally_item_groups(cfg) if any(v["type"] in ("Purchase", "Purchase (New)", "Purchase Transit") for v in fresh) else {}
	done = 0
	for start in range(0, len(fresh), cfg.read_batch):
		chunk = fresh[start : start + cfg.read_batch]
		details = tally_voucher_details(cfg, [v["guid"] for v in chunk])
		batch = []
		for v in chunk:
			d = details.get(v["guid"])
			if not d:
				log.warning("  %s %s was not returned by Tally this time; it will be tried again on the next run", v["type"], v["number"])
				continue
			for row in d["rows"]:
				row["item_group"] = groups.get(row["item"], "")
			batch.append(d)
		done += len(chunk)
		if not batch:
			continue
		res = erp_call(cfg, "dms_erp.warehouse.tally_vouchers.receive_tally_vouchers", http="POST", data={"vouchers": json.dumps(batch)})
		made += len(res.get("created") or [])
		new_items += res.get("newItems") or []
		new_batches += res.get("newBatches") or []
		already += res.get("alreadyInErp") or 0
		unplaced += res.get("unplaced") or []
		for why, count in (res.get("ignored") or {}).items():
			ignored[why] = ignored.get(why, 0) + count
		log.info("  %d/%d vouchers read and sent", done, len(fresh))
	log.info("draft documents made: %d (already in the ERP %d, not placed %d)", made, already, len(unplaced))
	if new_items or new_batches:
		log.info("  created for review: %d new item(s), %d new batch(es)", len(new_items), len(new_batches))
		for i in new_items[:20]:
			log.info("    new item: %s (group %s)", i["item"], i["group"])
	for why, count in ignored.items():
		log.info("  left alone x%s: %s", count, why)
	for u in unplaced[:20]:
		log.warning("  not placed: %s %s -- %s", u["type"], u["voucher"], u["reason"])


# ---------------------------------------------------------------- the schedule

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _hhmm(text):
	m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
	if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
		sys.exit(f"config.ini [schedule]: '{text}' is not a time (use HH:MM, 24-hour).")
	return int(m.group(1)), int(m.group(2))


def parse_schedule(items):
	"""The [schedule] section as {"daily": [(h, m)], "weekly": [(weekday, h, m)], "every": minutes,
	"between": ((h, m), (h, m))}, or None when nothing is set. Styles may be combined."""
	items = {k.lower(): v.strip() for k, v in items.items() if v and v.strip()}
	sched = {}
	if items.get("run_daily_at"):
		sched["daily"] = [_hhmm(t) for t in items["run_daily_at"].split(",")]
	if items.get("run_weekly"):
		weekly = []
		for part in items["run_weekly"].split(","):
			day, _, at = part.strip().partition(" ")
			if day[:3].lower() not in WEEKDAYS:
				sys.exit(f"config.ini [schedule] run_weekly: '{day}' is not a weekday (Mon, Tue, ...).")
			weekly.append((WEEKDAYS.index(day[:3].lower()), *_hhmm(at)))
		sched["weekly"] = weekly
	if items.get("run_every_minutes"):
		sched["every"] = max(1, int(items["run_every_minutes"]))
		if items.get("run_between"):
			lo, _, hi = items["run_between"].partition("-")
			sched["between"] = (_hhmm(lo), _hhmm(hi))
	return sched or None


def _in_window(moment, window):
	(h1, m1), (h2, m2) = window
	t, lo, hi = moment.hour * 60 + moment.minute, h1 * 60 + m1, h2 * 60 + m2
	return lo <= t <= hi if lo <= hi else (t >= lo or t <= hi)


def next_run(sched, now):
	"""The first time after `now` the schedule says to run (the earliest of its styles)."""
	candidates = []
	for h, m in sched.get("daily", []):
		at = now.replace(hour=h, minute=m, second=0, microsecond=0)
		candidates.append(at if at > now else at + datetime.timedelta(days=1))
	for weekday, h, m in sched.get("weekly", []):
		at = (now + datetime.timedelta(days=(weekday - now.weekday()) % 7)).replace(hour=h, minute=m, second=0, microsecond=0)
		candidates.append(at if at > now else at + datetime.timedelta(days=7))
	if sched.get("every"):
		at = (now + datetime.timedelta(minutes=sched["every"])).replace(second=0, microsecond=0)
		window = sched.get("between")
		if window and not _in_window(at, window):
			# outside the hours: the next time the window opens
			(h, m) = window[0]
			opens = at.replace(hour=h, minute=m)
			at = opens if opens > at else opens + datetime.timedelta(days=1)
		candidates.append(at)
	return min(candidates)


def describe_schedule(sched):
	parts = []
	if sched.get("daily"):
		parts.append("daily at " + ", ".join(f"{h:02d}:{m:02d}" for h, m in sched["daily"]))
	if sched.get("weekly"):
		parts.append("weekly " + ", ".join(f"{WEEKDAYS[d].title()} {h:02d}:{m:02d}" for d, h, m in sched["weekly"]))
	if sched.get("every"):
		window = sched.get("between")
		parts.append(f"every {sched['every']} min" + (f" between {window[0][0]:02d}:{window[0][1]:02d} and {window[1][0]:02d}:{window[1][1]:02d}" if window else ""))
	return "; ".join(parts)


def run_scheduled(cfg, sleep=time.sleep, clock=datetime.datetime.now, max_runs=None):
	"""Keep running: wait for the next time in [schedule], import the window from [vouchers], repeat. A run
	that fails (Tally closed, ERP unreachable) is logged and the schedule carries on with the next one;
	each import is safe to repeat because the ERP skips every voucher it already holds."""
	if not cfg.schedule:
		sys.exit("config.ini has no [schedule] (run_daily_at, run_weekly or run_every_minutes).")
	log.info("importing Tally vouchers %s", describe_schedule(cfg.schedule))
	runs = 0
	while max_runs is None or runs < max_runs:
		when = next_run(cfg.schedule, clock())
		log.info("next run at %s", when.strftime("%Y-%m-%d %H:%M"))
		while clock() < when:
			sleep(min(30, max((when - clock()).total_seconds(), 0.1)))
		since = resolve_since(cfg)
		log.info("run: vouchers dated %s or later", since or "the start of the year")
		try:
			send_vouchers(cfg, False, since, None, cfg.voucher_types)
		except urllib.error.HTTPError as e:
			log.error("ERP returned %s: %s", e.code, e.read()[:300])
		except (urllib.error.URLError, OSError) as e:
			log.warning("could not reach the ERP or Tally: %s", e)
		except Exception:  # by design: one bad run must not end the schedule
			log.exception("this run failed; the schedule carries on")
		runs += 1


def main():
	ap = argparse.ArgumentParser(description="DMS -> Tally sales invoice sync agent")
	ap.add_argument("--config", default="config.ini")
	ap.add_argument("--once", action="store_true", help="run one pass and exit")
	ap.add_argument("--dry-run", action="store_true", help="with --vouchers: show what is new, send nothing")
	ap.add_argument("--limit", type=int, help="with --vouchers: only the first N new vouchers (a quick trial)")
	ap.add_argument("--vouchers", action="store_true", help="read new Sales / Delivery Note / Purchase / receipt / payment / contra / credit and debit note vouchers from Tally and send them to the ERP as drafts (Delivery Note, Sales / Purchase Invoice and their returns, Payment Entry, Journal Entry), exit")
	ap.add_argument("--since", metavar="YYYYMMDD", help="with --vouchers: only vouchers dated this day or later (default: config.ini [vouchers] from_date / from_days_back)")
	ap.add_argument("--schedule", action="store_true", help="keep running and import vouchers at the times in config.ini [schedule]")
	ap.add_argument("--types", metavar="TYPE[,TYPE]", help="with --vouchers: only these Tally voucher types, e.g. 'Sales,Delivery Note' (default: all of them); a payment can only be allocated to an invoice that is already submitted, so importing invoices first, submitting them, then the receipts gives allocated receipts")
	args = ap.parse_args()
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
	cfg = Config(args.config)
	if args.schedule:
		run_scheduled(cfg)
		return
	if args.vouchers:
		try:
			since = _yyyymmdd(args.since) if args.since else resolve_since(cfg)
			types = _canon_types(args.types) if args.types else cfg.voucher_types
			send_vouchers(cfg, args.dry_run, since, args.limit, types)
		except urllib.error.HTTPError as e:
			hint = " -- the API key/secret in config.ini isn't valid on this ERP site" if e.code in (401, 403) else ""
			sys.exit(f"ERP {cfg.erp_url} answered {e.code}{hint}: {e.read()[:300].decode('utf-8', 'replace')}")
		except (urllib.error.URLError, OSError) as e:
			sys.exit(f"Could not reach the ERP or Tally: {e}")
		return
	log.info("syncing %s -> Tally %s (company %r), every %ss", cfg.erp_url, cfg.tally_url, cfg.company, cfg.interval)
	while True:
		try:
			sync_once(cfg)
		except urllib.error.HTTPError as e:
			log.error("ERP returned %s: %s", e.code, e.read()[:300])
		except (urllib.error.URLError, OSError) as e:
			log.warning("ERP not reachable: %s", e)
		if args.once:
			break
		time.sleep(cfg.interval)


if __name__ == "__main__":
	main()
