# Tally sync agent (ERP → Tally, Sales Invoices)

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

## Local testing on a developer PC

- `[erp] url = http://localhost:8000` (the local bench).
- If something else already listens on `localhost:9000` (the local bench's socketio
  port does), point `[tally] url` at this PC's own network address instead
  (e.g. `http://172.31.224.1:9000`).
- An **unlicensed (Educational mode)** TallyPrime only accepts vouchers dated the 1st,
  2nd or 31st of a month — set `educational_mode = yes` there and the agent moves each
  voucher to the 1st of its month (the real date goes in the narration). Never use it
  against the real, licensed Tally.
