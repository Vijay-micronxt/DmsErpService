# Setting up the Tally data

How to load the Tally-migrated data (a Frappe site backup taken from the local
`tally8.local` site on 8 Oct 2026) into a new site, and make `dms_erp` work on it.
Written for whoever has shell access to the bench. The worked example uses
`pacific-inc.storenxt.in`, where `dms_erp.auth.api.login` answered *"App dms_erp is
not installed"* and the Item screen failed with *"custom_series_ref is referring to
non-existing doctype Product Series"* -- the site had some of our setup but the app
was never installed on it. Step 5 fixes that.

**A restore replaces everything in the site's database.** Step 2 backs the site up
first; don't skip it.

Before you start: the three backup files are on the server; the bench has `frappe`,
`erpnext` and `india_compliance` (all version-15, the same apps the backup came
from); `apps/dms_erp` is on `testing/APIs` at `fecfef1` or later; you have the
MariaDB root password; nobody is using the site.

### Step 1. Set two variables and check what the site has

```bash
cd ~/frappe-bench
export SITE=pacific-inc.storenxt.in
export BK=/home/frappe/pacific_backup

bench --site $SITE list-apps
git -C apps/dms_erp log --oneline -3
```

Expect the app list to show `frappe` and `erpnext`, probably without `dms_erp`. The git
log should include `fecfef1` or a newer commit; if it doesn't, pull `testing/APIs`
first. If `india_compliance` is missing from `ls apps`, install it before going on, on
the branch that matches your Frappe version.

### Step 2. Back up the site as it is now

```bash
bench --site $SITE backup --with-files
```

Expect three new files under `sites/<site>/private/backups/`. Note their names; this is
your way back (see "If something goes wrong").

### Step 3. Put the Tally backup on the server

Copy these three files into `$BK` (for example with `scp`):

- `20261008_152743-tally8_local-database.sql.gz`
- `20261008_152743-tally8_local-files.tar`
- `20261008_152743-tally8_local-private-files.tar`

```bash
ls -lh $BK
```

Expect the database file to be the largest of the three. If a file is missing or has
size 0, stop and ask for it again. The folder may also hold a `site_config_backup.json`;
keep it.

### Step 4. Restore the backup into the site

```bash
bench --site $SITE restore \
  $BK/20261008_152743-tally8_local-database.sql.gz \
  --with-public-files  $BK/20261008_152743-tally8_local-files.tar \
  --with-private-files $BK/20261008_152743-tally8_local-private-files.tar \
  --db-root-password '<mariadb root password>'
```

If it refuses because the database already has tables, add `--force` and run it again.
That is expected for a site that already exists, and it is why Step 2 matters. Locally
this step took a few minutes. The Administrator password is now the old site's
password; Step 6 resets it.

### Step 5. Migrate, then install our app

```bash
bench --site $SITE migrate
bench --site $SITE list-apps
bench --site $SITE install-app dms_erp      # skip if dms_erp is already listed
bench --site $SITE migrate
```

Expect `list-apps` to show `frappe`, `erpnext`, `india_compliance` and `dms_erp` after the
install. The install creates our document types (including Product Series), the custom
fields and the roles; it does not delete the migrated data. Read the end of each
migrate's output. If any step prints an error or a traceback, stop and send it back --
a migrate that stops early is the usual reason a custom field is missing afterwards.

### Step 6. Reset the admin password and turn on the scheduler

```bash
bench --site $SITE set-admin-password '<new password>'
bench --site $SITE enable-scheduler
```

The scheduler runs the nightly jobs (dealer price tiers, the reorder digest, the dealer
inactivity check).

### Step 7. Add the login signing keys

Staff and dealer login sign their tokens with keys stored in the site config. A restored
site does not have them, so login fails until they are added.

```bash
KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
bench --site $SITE set-config dms_erp_jwt_active_kid k1
bench --site $SITE set-config dms_erp_jwt_keys "{\"k1\": \"$KEY\"}" --parse
```

The other site settings (WhatsApp tokens, the webhook secret, the AI provider keys) are
listed in `README.md` under "Required site_config.json keys"; add the ones this site needs. Also
open the backup's `site_config_backup.json` if you kept it: if it has an
`encryption_key` that differs from this site's, tell us before going further, because
passwords stored in the app could become unreadable.

### Step 8. Run the one-time data fix (dry run first)

The Tally migration put dealer phone and email, GST number and address in places the app
does not read. A one-time script, `backfill_tally_migrated_fields.py`, copies them
across. It only fills empty fields and is safe to run again. It is **not in this repo**
(it is a one-time migration fix, not app behaviour); it is sent separately.

```bash
bench --site $SITE console
>>> exec(open("/path/to/backfill_tally_migrated_fields.py").read(), globals())
>>> run(dry_run=True)     # prints what would change, writes nothing
>>> run(dry_run=False)    # only after you have checked the dry run
```

Send back the dry-run output before running it for real.

### Step 9. Clear the cache and restart

```bash
bench --site $SITE clear-cache
bench restart
```

On a server managed by supervisor, `sudo supervisorctl restart all` does the same. A
restart is needed: the running web processes keep the old setup until they are
restarted.

### Check that it worked

| Check | Expected |
|---|---|
| `bench --site $SITE list-apps` | Lists `dms_erp` |
| Open `https://pacific-inc.storenxt.in/app/item` in the admin | The Item list opens; no "Missing DocType" message |
| `curl -s -X POST https://pacific-inc.storenxt.in/api/method/dms_erp.auth.api.login` | Any answer except "App dms_erp is not installed" (a complaint about a missing email or password is fine) |
| Staff app login with a real staff user | Logs in; Product Catalog lists items |
| Row counts (SQL below) | Close to the local copy: 394 customers / 4,339 items / 16,898 sales invoices / 2,161 purchase invoices |

```bash
bench --site $SITE mariadb
```

```sql
SELECT (SELECT COUNT(*) FROM tabCustomer) AS customers,
       (SELECT COUNT(*) FROM tabItem) AS items,
       (SELECT COUNT(*) FROM `tabSales Invoice`) AS sales_invoices,
       (SELECT COUNT(*) FROM `tabPurchase Invoice`) AS purchase_invoices;

SELECT name FROM tabDocType WHERE name = 'Product Series';

SELECT fieldname FROM `tabCustom Field`
  WHERE fieldname IN ('custom_colour_attribute_1','custom_driver_mobile');
```

The last two queries should return one row for Product Series and two rows for the
custom fields. Zero rows means Step 5 did not complete; run it again and read its
output.

### If something goes wrong

Restore the backup from Step 2 the same way as Step 4, using those three files and
`--force`. That puts the site back as it was before you started.

### After it works

- The WhatsApp bot and the other apps still point at `tas-demo.storenxt.in`. Moving them
  to this site means changing the API address in the staff app, the dealer portal and
  each step of the WhatsApp Flow. Do that only when Pacific confirms the cut-over.
- The backup is from **8 Oct 2026**; anything entered after that date is not in it. Ask
  for a fresh backup before Step 3 if Pacific needs newer data.
- `docs/whatsapp-flows/dealer-portal-pacific.json` contains an API token in plain text.
  Rotate that token on the server once the Flow is moved.

When done, send back: the output of `list-apps` and of the last `migrate`, the row
counts from the checks above, the dry-run output from Step 8, and any error message
copied exactly.
