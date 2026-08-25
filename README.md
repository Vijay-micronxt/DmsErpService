# dms_erp — Pacific DMS

Custom Frappe app backing Pacific Inc's internal ops system (installed into an
existing ERPNext site). Serves the `pacific-tileflow` React/TanStack SPA over a
pure JSON API — no Frappe Desk, no cookies, no redirects.

## Status: Phase 0 — Staff auth

Only the `auth` module is implemented. `catalog`, `warehouse`, `purchase`,
`pricing`, `finance`, `comms` are empty placeholders (see each module's
README.md) reserved for later phases so they drop in without restructuring.

## Install

This container doesn't have `bench` installed, so the app was hand-scaffolded
to match what `bench new-app` would produce. To install it for real:

```bash
bench get-app dms_erp /path/to/this/repo   # or a git URL
bench --site <site-name> install-app dms_erp
bench --site <site-name> migrate           # creates the Auth Session table + Pacific roles
```

`erpnext` should already be installed on the target site before later phases
(warehouse/purchase) land, since those build on ERPNext's native Warehouse/Bin/
Stock Ledger doctypes rather than a custom stock model. Phase 0 has no ERPNext
dependency.

## Required site_config.json keys

JWT signing keys are **not** auto-generated — set them explicitly so a fresh
`bench new-site` doesn't silently mint a throwaway secret:

```bash
bench --site <site-name> set-config dms_erp_jwt_keys '{"k1": "<a long random secret>"}' --parse
bench --site <site-name> set-config dms_erp_jwt_active_kid k1
```

Optional (defaults shown):

```json
{
  "dms_erp_access_token_ttl": 2400,
  "dms_erp_refresh_token_ttl_days": 30
}
```

**Key rotation:** add a new kid to `dms_erp_jwt_keys`, point
`dms_erp_jwt_active_kid` at it. Keep the old kid's secret in the map until every
access token signed with it has expired (`dms_erp_access_token_ttl` seconds
after the rotation), then remove it.

## Roles

Four Frappe Roles are created automatically on install/migrate, all with
`desk_access = 0` (staff users never need `/app`):

| Frontend role | Frappe Role         |
|---------------|----------------------|
| sales         | `Pacific Sales`      |
| warehouse     | `Pacific Warehouse`  |
| purchase      | `Pacific Purchase`   |
| management    | `Pacific Management` |

`System Manager` is also accepted as a login-time escape hatch for admin
accounts, but every real staff user should be assigned one of the four roles
above.

## API

All endpoints are under `/api/method/dms_erp.auth.api.<method>`. `login` and
`refresh_token` are the only unauthenticated (`allow_guest`) endpoints;
everything else requires `Authorization: Bearer <access_token>`.

| Method | Auth required | Body / params |
|---|---|---|
| `login` | no | `usr`, `pwd`, `device_id`, `device_name?` |
| `refresh_token` | no | `refresh_token` |
| `logout` | no | `refresh_token` |
| `logout_all` | yes | — |
| `me` | yes | — |

`login` and the successful path of `refresh_token` return:

```json
{
  "access_token": "<jwt>",
  "refresh_token": "<opaque>",
  "token_type": "Bearer",
  "expires_in": 2400,
  "user": {
    "name": "jane@pacific.example",
    "email": "jane@pacific.example",
    "full_name": "Jane Doe",
    "roles": ["Pacific Warehouse"],
    "app_roles": ["warehouse"],
    "primary_role": "warehouse"
  }
}
```

(`user` is omitted from `refresh_token`'s response — call `me` if the client
needs a fresh profile.)

Example:

```bash
curl -X POST https://<site>/api/method/dms_erp.auth.api.login \
  -H 'Content-Type: application/json' \
  -d '{"usr":"jane@pacific.example","pwd":"...","device_id":"pos-terminal-1","device_name":"Warehouse iPad"}'

curl https://<site>/api/method/dms_erp.auth.api.me \
  -H 'Authorization: Bearer <access_token>'
```

### Refresh token rotation & reuse detection

Every call to `refresh_token` consumes the presented token and issues a new
one. If a refresh token that was already rotated out gets presented again
(replay of a stolen token), the session is revoked immediately rather than
issued new tokens.

## Assumptions made (please confirm/correct)

Since this repo started blank with no bench/mariadb/frappe available in this
container, the following were assumed and are worth confirming before Phase 2:

- **Frappe/ERPNext version**: targeting a recent v15-line site (uses
  `pyproject.toml`-style app layout, not the older `setup.py` one). If you're
  on v13/v14, the app still works but you may want a `setup.py` alongside.
- **Environment**: plain `bench` (local or Docker-based `frappe_docker`), not
  Frappe Cloud — nothing here is Frappe-Cloud-specific either way.
- **Target site name**: not pinned down anywhere in the app; you supply it at
  `bench --site <site> install-app` time.
- **App name**: `dms_erp` (derived from the repo name `DmsErpService`), title
  "Pacific DMS". Renaming later is a bigger diff (module paths, doctype
  `module` field) than fixing it now if it's wrong.
- **License**: `hooks.py` sets `Proprietary`; change if you want something
  else on record.
- **Login is restricted** to users holding one of the four Pacific roles (or
  System Manager) — an ERPNext accounting/sales user with no Pacific role
  cannot log into the staff app even with valid Frappe credentials. Flag if
  you wanted this open to any enabled User instead.
