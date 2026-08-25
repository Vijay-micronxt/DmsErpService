# Comms

Phase 7: WhatsApp communication hooks. Implemented.

**WhatsApp Message** (`api.py`, `doctype/whatsapp_message`) — a genuine custom
doctype. Frappe's own `Communication` doctype is the nearest built-in and was
considered, but its `status` vocabulary (Open/Closed/Replied — thread-handling
state) doesn't model WhatsApp's delivery-receipt states (Sent/Delivered/Read/
Failed), and "WhatsApp" isn't a stock `communication_medium` option on a doctype
shared by unrelated core features — extending it would mean customizing a
widely-used shared doctype rather than modeling Pacific's own lifecycle.

The real WhatsApp Business API send/receive integration is middleware-side per
the BRD — this module is the system of record only: it logs the thread, lets
staff trigger an outbound send, and exposes a webhook contract
(`webhook_inbound_message`, `webhook_status_update`) for that (not-yet-built)
middleware to post inbound messages and real delivery receipts back. There's no
fake "Delivered after 1.2s" timeout like the frontend's UI-only simulation — a
real status progression can only come from the actual transport layer.

The webhook endpoints are `allow_guest` (no staff JWT — middleware isn't a
logged-in user) but gated behind a shared secret in `site_config.json`
(`dms_erp_whatsapp_webhook_secret`), the same pattern as Phase 0's JWT signing
keys. That's a placeholder, not the real auth scheme — swap it for whatever the
actual middleware integration ends up using (Meta's verify-token handshake, an
HMAC signature, an IP allowlist) once that's built; leaving these endpoints open
to any anonymous caller wasn't an option.

Message templates are a static list (`utils.py`), same as the frontend — not a
doctype, since nothing here needs them editable without a deploy yet.

Sales/Management send messages and mark inbound ones read; everyone reads.
