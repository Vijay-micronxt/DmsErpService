# assistant

Staff-app AI chat (31 Oct 2026 client request): a slide-out panel in the staff app
for general questions over ERP data, plus "upload an invoice/quotation photo and
create the document from it" -- the same capability the separately-installed
`pilot_assist` app already gives Frappe Desk users via its own chat widget, just
exposed to the staff app's own frontend instead.

## Why a proxy, not a rebuild

`pilot_assist` already has every piece of this built (`pilot_assist.api.ai_api`:
`chat_api`, `ask_document`, `match_items`, `create_purchase_invoice_from_data`,
`create_quotation_from_data`, `speech_to_text`) -- multi-provider AI calls, image
handling, field extraction, item matching, the lot. None of that is reimplemented
here. `ai_assistant_api.py` is a thin pass-through: each endpoint takes the staff
app's request and calls straight into the matching `pilot_assist` function.

## Why no separate auth bridge was needed

`pilot_assist`'s endpoints expect Frappe's own native login (session cookie or an
`Authorization: token <api_key>:<api_secret>` header) -- not this app's JWT. No new
auth plumbing was built to bridge that, because none was needed: `dms_erp.auth.
middleware.authenticate_request` already calls `frappe.set_user(...)` for every
authenticated staff-app request, before any whitelisted function (including these)
runs. `pilot_assist` is installed on the same site/process, so these wrappers call
its functions directly, in-process -- by the time that call happens, Frappe already
sees a normal logged-in user, exactly as if they'd reached it any other way. The
same middleware already confines DMS Dealer-only accounts to the dealer portal API
paths (see its own docstring), and this module isn't one of them, so dealer
accounts are refused automatically with nothing to check here either.

## Prerequisites this module doesn't create

Both are `pilot_assist`/`frappe_assistant_core`'s own requirements, unrelated to
this proxy:
- Each staff user needs a Frappe API key + secret generated on their own User
  record (User > API Access > Generate Keys) -- `pilot_assist` uses that key
  internally to call its own MCP data layer as that user.
- "Enable Assistant Access" (a checkbox `pilot_assist` adds to the User form,
  ticked by default) must stay on for that user.

If `pilot_assist` isn't installed on a site, every wrapper here throws a clear
"isn't installed" error on first use rather than failing at import time -- the
import is lazy specifically so dms_erp itself still boots fine without it.

## The image -> document flow

`ask_with_image` is the only one of these that actually reads an uploaded image --
same `ask_document` endpoint `pilot_assist`'s own Desk widget calls. Its response is
a plain string; when the AI reads the image as an invoice/quotation it's prefixed
`PI_PREVIEW:`/`QUOTE_PREVIEW:` followed by JSON (mapped fields, items, taxes). The
frontend shows that as an editable preview -- `match_items` lets it resolve each
extracted item name against the real Item master before anything is created -- and
only once the user confirms does `create_purchase_invoice`/`create_quotation` get
called, with that (possibly corrected) data round-tripped back. Same two-step
review-before-create flow the widget itself uses, not collapsed into one call.
