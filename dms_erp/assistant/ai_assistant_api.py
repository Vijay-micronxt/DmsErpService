"""Thin staff-app proxy onto the separately-installed `pilot_assist` app's AI
assistant (chat API, same site, same process). No second auth system needed here:
dms_erp.auth.middleware already resolves the staff app's own JWT to a real
frappe.session.user before any whitelisted function runs (see that module's
docstring), and already blocks DMS Dealer-only accounts from reaching anything
outside the dealer portal API paths -- this module isn't one of those paths, so
dealer accounts are refused automatically, with nothing to check here.

pilot_assist itself is a separate, optionally-installed app (not every site will
have it) -- every wrapper below imports it lazily, inside the function, so a site
without pilot_assist installed still boots dms_erp cleanly; only calling one of
these endpoints on such a site fails, with a clear message instead of an import
crash at startup.

Each staff user who uses this needs their own Frappe API key/secret generated
(User > API Access > Generate Keys) and "Enable Assistant Access" ticked on their
User -- both are pilot_assist/frappe_assistant_core's own requirements (it uses
that key internally to call its MCP data layer as that user), not something new
introduced here."""

import frappe
from frappe import _


def _pilot_assist_api():
	try:
		from pilot_assist.api import ai_api
	except ImportError:
		frappe.throw(_("The AI assistant isn't installed on this site."), frappe.ValidationError)
	return ai_api


@frappe.whitelist(methods=["POST"])
def chat(message: str, history: str | None = None, selected_role: str | None = None) -> str:
	"""General Q&A over ERP data, no image -- routed through ask_document, the SAME
	endpoint pilot_assist's own Desk widget calls for every message (confirmed from
	its JS: it never calls chat_api at all). This used to call chat_api instead --
	a separate, much thinner implementation meant for stateless external callers,
	which forced the AI into a rigid JSON schema (so list/table answers reduced to
	a one-line summary with the row data dropped), never offered a role picker on
	a role-based-AI site, and never produced a "Load More" pageable list or a
	"[Open X ->]" document link. Switching to ask_document closes all of that at
	once, in exchange for the response now being plain text (markdown tables, a
	"ROLE_SELECT:"/"LIST_DATA:"-prefixed payload sometimes) instead of pre-parsed
	JSON -- src/api/assistant.ts and ai-assistant-panel.tsx on the frontend handle
	that the same way they already do for ask_with_image below."""
	return _pilot_assist_api().ask_document(
		message=message, doctype=None, docname=None, history=history, selected_role=selected_role
	)


@frappe.whitelist(methods=["POST"])
def ask_with_image(
	message: str,
	image_data: str | None = None,
	image_mime: str | None = None,
	history: str | None = None,
	selected_role: str | None = None,
) -> str:
	"""The only pilot_assist endpoint that actually reads an uploaded image/PDF --
	ask_document, same one its own Desk widget calls. Returns a plain string;
	when the AI reads the image as an invoice/quotation it comes back prefixed
	"PI_PREVIEW:"/"QUOTE_PREVIEW:" followed by JSON (mapped fields, items, taxes)
	for the frontend to show as an editable preview card before create_purchase_
	invoice/create_quotation actually creates anything -- same two-step flow the
	widget itself uses, deliberately not collapsed into one call here, so the
	staff user always sees and can correct what was extracted before anything
	is actually created. selected_role: see get_role_choices below -- without it,
	a user matching 2+ configured roles under role-based AI mode got back a raw,
	unhandled "ROLE_SELECT:" string instead of ever being able to answer."""
	return _pilot_assist_api().ask_document(
		message=message, doctype=None, docname=None, image_data=image_data, image_mime=image_mime,
		history=history, selected_role=selected_role,
	)


@frappe.whitelist(methods=["GET"])
def get_role_choices() -> dict:
	"""Mirrors what pilot_assist's own boot_session hook puts in bootinfo.ai_role_choices
	for the Desk widget's header dropdown (pilot_assist_widget.js reads frappe.boot.
	ai_role_choices directly) -- the React app has no Frappe bootinfo of its own to read
	that from (it's a separate SPA authenticating over this app's own JWT, not a Desk
	session), so this exposes the same _get_matched_role_choices() as its own endpoint.
	Empty list means "no picker needed" -- either role-based AI is off, this user matched
	0 or 1 configured role, or their own Enable Assistant Access is off (see that function's
	own docstring in pilot_assist for the exact rules)."""
	api = _pilot_assist_api()
	return {"roles": api._get_matched_role_choices(frappe.session.user)}


@frappe.whitelist(methods=["POST"])
def load_more(doctype: str, filters: str, fields: str, offset: int, page_size: int = 10, total: int | None = None) -> dict:
	"""Backs the chat panel's "Load More" button on a LIST_DATA: response from chat/
	ask_with_image above -- same query the AI already decided on for the first page,
	re-run directly via MCP with a larger limit, no AI call involved (see pilot_assist's
	own load_more_list_records docstring)."""
	return _pilot_assist_api().load_more_list_records(
		doctype=doctype, filters=filters, fields=fields, offset=offset, page_size=page_size, total=total
	)


@frappe.whitelist(methods=["POST"])
def match_items(names: str) -> dict:
	"""How each item name the AI extracted from an image relates to the real Item
	master, so the preview card can show/let the user fix the match before the
	invoice line is actually created from it."""
	return _pilot_assist_api().match_items(names=names)


def _backfill_title(doctype: str, name: str, *fieldnames: str) -> None:
	"""pilot_assist saves with doc.flags.ignore_validate = True (so a messy AI-extracted
	document can still save as a Draft instead of hard-failing on ERPNext's business-rule
	checks) -- a side effect is that ERPNext's own title-field logic, which normally runs
	during validate(), never runs either, so the record's `title` stays blank and list
	views fall back to showing its name (PINV-26-00001) instead of the party name. The
	underlying data (supplier/customer) is already correct; this only fixes that display
	gap, and deliberately doesn't touch pilot_assist's validation-skip behavior at all."""
	values = frappe.db.get_value(doctype, name, list(fieldnames), as_dict=True) or {}
	title = next((values[f] for f in fieldnames if values.get(f)), None)
	if title:
		frappe.db.set_value(doctype, name, "title", title, update_modified=False)


@frappe.whitelist(methods=["POST"])
def create_purchase_invoice(data: str) -> dict:
	"""Confirms a PI_PREVIEW the user has reviewed (and possibly corrected) in
	the UI -- `data` is that same mapped_data, round-tripped back."""
	result = _pilot_assist_api().create_purchase_invoice_from_data(data=data)
	if result.get("name"):
		_backfill_title("Purchase Invoice", result["name"], "supplier_name", "supplier")
	return result


@frappe.whitelist(methods=["POST"])
def create_quotation(data: str) -> dict:
	"""Confirms a QUOTE_PREVIEW the user has reviewed (and possibly corrected)
	in the UI -- `data` is that same mapped_data, round-tripped back."""
	result = _pilot_assist_api().create_quotation_from_data(data=data)
	if result.get("name"):
		_backfill_title("Quotation", result["name"], "customer_name", "party_name")
	return result


@frappe.whitelist(methods=["POST"])
def speech_to_text(audio_data: str) -> dict:
	return _pilot_assist_api().speech_to_text(audio_data=audio_data)


@frappe.whitelist(methods=["GET"])
def site_branding() -> dict:
	"""Client request, 31 Oct 2026 -- the AI panel header should show this site's own
	branded name, matching pilot_assist's own Desk widget exactly rather than inventing
	a separate scheme: that widget builds its title from frappe.boot.sysdefaults.company
	(see pilot_assist_widget.js, `const ai_title = _company ? ('I' + _company) : ...`),
	i.e. the default Company's name prefixed with a capital I -- not Website Settings'
	app_name, and not a lowercase i."""
	company = frappe.defaults.get_global_default("company")
	name = f"I{company}" if company else "AI Assistant"
	return {"appName": name}
