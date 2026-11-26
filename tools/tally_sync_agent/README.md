# Tally sync agent (ERP → Tally, Sales Invoices)

> **Status: disabled — still in development.** Not active on any site until it is fully completed and signed off: the Sales Invoice hooks are commented out in `hooks.py`, the Tally fields are not created on `bench migrate`, and the agent endpoints refuse (`tally_sync.TALLY_SYNC_ENABLED = False`). Nothing in any other flow runs Tally code. Don't install this on the Tally PC until then — the ERP will refuse it.

Runs on the PC where **TallyPrime** is open. Every `interval_seconds` it asks the DMS
ERP for submitted Sales Invoices ticked **Sync to Tally**, creates each one in Tally as
a Sales voucher, and writes the result back on the invoice (status, Tally voucher
number, or Tally's error message). Why it works this way — the cloud ERP can't reach
an office PC and Tally can't push — is in `dms_erp/sales/tally_sync.py`.

## One-time setup

1. **Python 3.10+** on the Tally PC (python.org installer, tick "Add to PATH").
2. **TallyPrime connectivity:** F1 (Help) → Settings → Connectivity →
   *TallyPrime acts as* **Both** (or Server), port **9000**. Restart TallyPrime and keep
   the company open.
3. **ERP API user:** in the ERP create a user with the **Accounts Manager** role,
   open it → *API Access* → **Generate Keys**, and copy the key and secret.
4. `copy config.example.ini config.ini` and fill in:
   - `[erp] url` — the site (e.g. `https://pacific-inc.storenxt.in`), `api_key`, `api_secret`
   - `[tally] url` — `http://localhost:9000` on the Tally PC itself
   - `[tally] company` — exactly as TallyPrime shows it, spaces included
     (`Pacific Inc  -  (From 1-Apr-2025)`)
   - leave `educational_mode = no` on a licensed Tally
5. Customers, items, godowns, batches and ledgers must already exist in Tally with the
   same names (true for everything the migration brought over). A missing one makes the
   invoice **Failed** with Tally's own message; fix it in Tally, then untick and re-tick
   *Sync to Tally* on the invoice to retry.

## Run

```
python agent.py --config config.ini          # keeps running (double-click run_agent.bat)
python agent.py --config config.ini --once   # one pass, then exit
```

`config.ini` holds an API secret: keep it on the Tally PC only (it is gitignored).

### Vouchers: Tally → ERP (draft Delivery Notes, Sales / Purchase Invoices, Payment Entries, Journal Entries)

```
python agent.py --config config.ini --vouchers --dry-run               # what is new, nothing sent
python agent.py --config config.ini --vouchers --since 20260822        # send, from this date
python agent.py --config config.ini --vouchers --limit 20              # a small trial
python agent.py --config config.ini --vouchers --types "Sales,Delivery Note"   # only these Tally voucher types
```

Carries on from the migration (history is in the ERP up to 21 Aug 2026, each document tagged with
its Tally voucher id): it lists the year's vouchers (ids and headings only, ~13 s), asks the ERP
which it does not hold, reads only those in full (~4 s each, with their tax lines, party and
sales/purchase ledger) and sends them to `dms_erp.warehouse.tally_vouchers.receive_tally_vouchers`,
which makes one **draft** document per voucher, shaped like the migrated ones. Off until the site
key `dms_erp_tally_vouchers_enabled` is set. Nothing is submitted; a person checks and submits.

| Tally voucher | ERP draft | Stock |
|---|---|---|
| Delivery Note | Delivery Note | goes out (where Tally moves it) |
| Sales raised against a delivery note (lines carry its tracking number) | Sales Invoice, update stock **off** | none -- the delivery note already did it |
| Sales, direct | Sales Invoice, update stock **on** | goes out |
| Purchase / Purchase (New) / Purchase Transit | Purchase Invoice, update stock **on** | comes in, into the godown on the line |
| Bank Receipt / Cash Receipt from a dealer | Payment Entry (Receive) | none |
| Bank Payment / Cash Payment / Payment to a supplier or transporter | Payment Entry (Pay) | none |
| Credit Note / Credit Note (Purchase) | Sales Invoice **return**; with stock lines update stock **on** (stock comes back in), **off** when the lines carry a delivery note's tracking number or it is ledger-only | back into the godown, where Tally moves it |
| Debit Note / Debit Note (Sales) | Purchase Invoice **return**; with stock lines update stock **on** (stock goes out), ledger-only: off | out of the godown on the line |
| Any of those against another ledger (expense, advance, several parties, reverse direction) and every Contra | Journal Entry (Bank / Cash / Contra Entry) | none |

The e-invoice and transport details come across too, into the same Tally fields the migration filled: IRN and its acknowledgement, e-way bill number, vehicle number, transporter, LR number and date, who entered the voucher, and on a purchase the driver, vehicle owner, permit and transporter GSTIN/PAN. Pacific keeps these in custom fields of its Tally (`UDF:_UDF_<number>`); the numbers are in `TRANSPORT_UDFS` in `agent.py` and belong to this company, so another Tally company needs its own. A field the document type lacks, or that Tally left blank, is simply not set.

Rates are the line amount over its quantity and Tally's tax / round-off ledgers become fixed-amount
tax rows (as on a sale, reversed on a purchase), so each invoice's total ties to Tally's -- the
reply shows both. A **purchase** may bring an item or batch the ERP lacks: the item is created (its
Tally stock group as Item Group; made purchase-only until it has a 6- or 8-digit HSN, listed as
`needsHsn`) and so is the batch, both returned for review. Everything else unknown -- customer,
supplier, godown, account, or an item/batch on a **sale** -- is reported and the whole voucher is
not made. A cancelled voucher is left alone; stock journals are not imported yet.

**Same as the migration, field by field.** An end-to-end test deleted 58 migrated documents of every type
this imports from a local copy, pulled them again from Tally through this agent, submitted them and compared
each with the original: stock entries and accounting entries identical on every one that could be submitted,
and every Tally field matching, including the audit trail the migration kept -- who entered and approved a
voucher and when, every alteration with its reason (`tally_alterations`), the customer classification, the
bill name, and per line the sq mtr, weight, item type, reservation status, rate and discount (Pacific's custom
fields, read by id from `AUDIT_UDFS` / `LINE_UDFS` in `agent.py`; another Tally company has other ids).
Details that follow from that test:

- A customer's / supplier's **own** receivable / payable account is used (the migration made alias parties with
  accounts of their own), not the company default.
- A bill's date is Tally's **reference date**; Tally's own round-off is the only rounding
  (`disable_rounded_total`), prices are final (`ignore_pricing_rule`), and India Compliance's "LR date = today"
  default is cleared.
- Within a day, **purchases are posted first, then credit notes, delivery notes, sales, debit notes**
  (01:00:00, a second apart): stock that comes in is on the books before stock that goes out of it, as in the
  migration; otherwise a same-day stock-out could land ahead of its stock-in and be refused.
- A **sale or purchase with no stock lines** (mobile maintenance, tile cutting, software add-ons) is the
  `TALLY-LEDGER-LINE` item, one line per ledger, update stock off.
- **Not repeatable by design**: document names (a new draft is `SINV-26-…`, the migration used the Tally
  number, which is kept in `tally_voucher_no`); the quantity text, shown "16 Box 3 Pcs" as asked; and what
  ERPNext works out itself (item weights, GST category, stock at the time, document status).
- Known limit: Tally lets a godown go negative, ERPNext does not, so a stock-out from a batch with no stock
  in that godown stays a draft and says so at submit (seen on debit note DN/0018).

**Credit and debit notes** (`_build_return` in `tally_vouchers.py`) are shaped like the 147 the migration
made this year (checked on 55 real vouchers, 48 built and compared field by field: items, quantities,
batches, godowns, accounts, tax rows, party, totals; all identical but a one-paisa rate on one line, and
every total ties to Tally's). Which document is decided by the voucher type the way the migration did it
-- a "Credit Note" of either kind is a Sales Invoice return, a "Debit Note" of either kind a Purchase
Invoice return -- not by who the party is. Quantity and amount are negative with a positive rate. A note
with no stock lines (rate difference, damages, shortage) has one `TALLY-LEDGER-LINE` line per ledger, with
only Tally's Tax and Round Off ledgers as tax rows; that item is made once if missing and flagged as a
sales and purchase item, because ERPNext refuses it otherwise. A ledger Tally lists both for the lines
and as an extra charge (a debit note's second "Damages Claim") keeps the extra as a charge. The return is
not linked to the invoice it reverses (the migration did not either); Tally's reference is in
`tally_reference`. A purchase voucher with no stock lines is reported ("no stock lines"), not made.

When submitting a return, ERPNext may refuse with "negative stock": a debit note takes goods out, and if
that batch is not in that godown any more, the draft stands and says so.

**Receipts, payments, contra** (`dms_erp/warehouse/tally_money.py`) are shaped exactly like the migrated
ones -- checked on 50 real vouchers (document type, party, amount, accounts, date all identical, and the
accounting entries identical once submitted). One bank/cash ledger against one dealer or supplier is a
Payment Entry, tied to the invoice(s) Tally says it settles when they are submitted in the ERP and still
owe (a supplier payment may settle a purchase invoice or a transporter's freight bill, which is a Journal
Entry); the bank line's cheque / transfer number and date become the reference (the voucher number when
there is none). A ledger with its own Account is an account, never a party (the migration made placeholder
customers named "Cash" and the bank names), and where the chart has a group of the same name the
"<name> (Ledger)" leaf is used. If a voucher's bills run against its direction or add up to more than was
paid (Tally netted an earlier receipt in it), nothing is allocated and the Remarks say so. An unknown
ledger stops that voucher and is named; no account is ever invented.

### What window to import, and when (config.ini)

```
[vouchers]
from_days_back = 7          ; the window starts this many days before the run ...
; from_date = 2026-08-15    ; ... or on this date (wins if both are set)
types = Delivery Note, Sales, Purchase, Credit Note, Debit Note, Bank Receipt, Bank Payment, Cash Receipt, Cash Payment, Payment, Contra
                            ; blank = all of them; "Credit Note (Purchase)" is found despite Tally's stray space
read_batch = 25             ; vouchers read from Tally per request

[schedule]                  ; pick one style, or combine them
run_daily_at = 22:00        ; one or more times, comma separated
; run_weekly = Sun 23:00
; run_every_minutes = 30
; run_between = 07:00-22:00 ; only with run_every_minutes (may cross midnight)
```

`python agent.py --config config.ini --vouchers` imports the window once (`--since`, `--types` override the
config); `python agent.py --config config.ini --schedule` keeps running and does it at the times in
`[schedule]` (a failed run -- Tally closed, ERP unreachable -- is logged and the next one is made as
planned). Running overlapping days is safe: the ERP skips every voucher it already holds.

**On the Tally PC:** the PC and Tally (with the company open) must be on at the scheduled time. Easiest is
Windows Task Scheduler: Create Task > trigger "At startup" (or "At log on") > action "Start a program" =
`run_agent.bat` (or `python agent.py --config config.ini --schedule`), "Start in" = this folder > tick
"Run whether user is logged on or not" > on the Settings tab "If the task fails, restart every 5 minutes".
Pick run times when few people use Tally: every run makes Tally scan the whole year for about 10 seconds,
during which it answers nobody else.

**Backdated vouchers.** A window counts the voucher's own date, so a bill typed today but dated three weeks
ago is missed by a 7-day window. Keep `from_days_back` longer than your longest normal backdating, or add
a nightly run with a wide window (a second agent with its own config.ini, `from_days_back = 30`).

**How it keeps Tally light** (measured on a year of 12,468 vouchers):

- *Date window.* Tally ignores the from / to dates sent with a request and answers for the whole financial
  year, and a two-sided date formula or arithmetic on the date is refused. A one-sided formula (year, then
  month, then day, "on or after") is applied exactly and is what `since_formula` sends; the end of the
  window is applied here. Tally still scans every voucher (8 to 13 s whatever the window), so the window
  shrinks the reply, not the scan. Day Book returns the same day whatever date is asked.
- *Batch reads.* Finding a voucher by id costs Tally one scan however many it is asked for, so 12 vouchers in
  one request took 9.8 s against 9.1 s *each* one by one. New vouchers are read `read_batch` at a time.
- *Cancelled and optional vouchers* are marked in the headings and are never read in full (they are never in
  the ERP, so they used to be re-read on every run).
- Never ask Tally for the lines of thousands of vouchers at once -- that stalls it.

## Local testing on a developer PC

- `[erp] url = http://localhost:8000` (the local bench).
- If something else already listens on `localhost:9000` (the local bench's socketio
  port does), point `[tally] url` at this PC's own network address instead
  (e.g. `http://172.31.224.1:9000`).
- An **unlicensed (Educational mode)** TallyPrime only accepts vouchers dated the 1st,
  2nd or 31st of a month — set `educational_mode = yes` there and the agent moves each
  voucher to the 1st of its month (the real date goes in the narration). Never use it
  against the real, licensed Tally.
