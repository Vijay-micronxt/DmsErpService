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
def chat(message: str, conversation_history: str | None = None) -> dict:
	"""General Q&A over ERP data, no image. See pilot_assist.api.ai_api.chat_api's
	own docstring for the response shape -- passed through unchanged."""
	return _pilot_assist_api().chat_api(message=message, conversation_history=conversation_history)


@frappe.whitelist(methods=["POST"])
def ask_with_image(
	message: str,
	image_data: str | None = None,
	image_mime: str | None = None,
	history: str | None = None,
) -> str:
	"""The only pilot_assist endpoint that actually reads an uploaded image/PDF --
	ask_document, same one its own Desk widget calls. Returns a plain string;
	when the AI reads the image as an invoice/quotation it comes back prefixed
	"PI_PREVIEW:"/"QUOTE_PREVIEW:" followed by JSON (mapped fields, items, taxes)
	for the frontend to show as an editable preview card before create_purchase_
	invoice/create_quotation actually creates anything -- same two-step flow the
	widget itself uses, deliberately not collapsed into one call here, so the
	staff user always sees and can correct what was extracted before anything
	is actually created."""
	return _pilot_assist_api().ask_document(
		message=message, doctype=None, docname=None, image_data=image_data, image_mime=image_mime, history=history
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
	configured name, not a hardcoded label. Whatever's actually set in Website Settings
	(falls back to the site's bench name if that's blank, same as Frappe itself does)."""
	name = frappe.db.get_single_value("Website Settings", "app_name") or frappe.local.site
	return {"appName": name}
