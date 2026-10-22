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
"""

import argparse
import configparser
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
	return raw.decode("utf-8", errors="replace")


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


def main():
	ap = argparse.ArgumentParser(description="DMS -> Tally sales invoice sync agent")
	ap.add_argument("--config", default="config.ini")
	ap.add_argument("--once", action="store_true", help="run one pass and exit")
	args = ap.parse_args()
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
	cfg = Config(args.config)
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
