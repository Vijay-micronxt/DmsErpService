"""LLM-based intent classification for inbound WhatsApp messages — deliberately not a
hand-maintained keyword dictionary. Dealer messages are free-form and often Hinglish/
transliterated ("stock hai kya", "milega kya", "available h?"), and no fixed keyword
list keeps up with real phrasing; an LLM classification call generalizes across
phrasing the way a dictionary never can.

This module's only job is turning free text into a small structured intent + the raw
substring the message seems to name — it never itself answers "is it in stock" or
"what's the price". That always stays a deterministic DB lookup in comms.api's
auto-reply path (catalog.api.resolve_item_mention + warehouse.utils.
total_stock_for_item), the same way sales.inquiry_api._create_inquiry derives status
from real on-hand qty rather than guessing. A classification mistake here means "the
bot didn't understand" (the message is left inbound-unreplied for a human), never
"the bot told a dealer something confidently wrong" — see _parse_intent_response's
strict validation, which refuses anything that doesn't match the expected shape
rather than trying to salvage a partial/malformed response.

Two providers, chosen by `dms_erp_llm_provider` in site_config.json ("anthropic" or
"gemini"; anything else — or missing config for the selected provider — disables
auto-classification and every inbound message just waits for a human, exactly like
before this module existed):

  dms_erp_llm_provider          -- "anthropic" or "gemini"
  dms_erp_anthropic_api_key     -- Anthropic API key
  dms_erp_anthropic_model       -- defaults to "claude-haiku-4-5" (cheap, fast --
                                     classification doesn't need a frontier model)
  dms_erp_gemini_api_key        -- Google AI Studio / Gemini API key
  dms_erp_gemini_model          -- defaults to "gemini-2.5-flash"; verify this is
                                     still a live model id for your account before
                                     relying on it (not verified live in this repo's
                                     dev environment — network access to Google's own
                                     docs was blocked here; Gemini model ids change
                                     more often than Anthropic's, so this is the one
                                     part of this module most likely to need a
                                     site_config update rather than a code change)

Raw HTTP via `requests`, matching comms.whats91's existing pattern — not the
provider SDKs, to avoid adding two more pip dependencies to a Frappe bench for one
small classification call. Both provider calls follow whats91.py's own contract:
never raise, log and return None on any failure (missing config, network error, bad
response, unparseable/invalid JSON) so an LLM outage or misconfiguration never breaks
the inbound webhook — it just quietly stops auto-replying, same as a whats91 outage
never breaks request_otp.
"""

import json

import frappe
import requests

_REQUEST_TIMEOUT_SECONDS = 15

VALID_INTENTS = {"availability_check", "price_check", "other"}

_SYSTEM_PROMPT = (
	"You classify a single inbound WhatsApp message from a tile dealer to a distributor. "
	"Reply with ONLY a JSON object, no other text, matching exactly this shape: "
	'{"intent": "availability_check" | "price_check" | "other", "item_mention": string or null}. '
	'"availability_check" is any question about whether/how much stock is available, in any '
	'language or transliteration (e.g. Hindi/Hinglish like "stock hai kya", "available h", '
	'"milega kya"). "price_check" is a question about price/rate. Use "other" for anything else '
	"(complaints, payment talk, greetings, unrelated chat) — when in doubt, prefer \"other\" "
	"rather than guessing. item_mention is the exact substring of the message that names the "
	"item/product/code being asked about, copied verbatim and unmodified, or null if no "
	"specific item is named. Only ever return the JSON object, nothing else."
)


def _parse_intent_response(raw_text: str) -> dict | None:
	"""Strict on purpose — anything that doesn't match the exact expected shape is
	treated as "couldn't classify" (None), not coerced into a best guess."""
	try:
		parsed = json.loads((raw_text or "").strip())
	except ValueError:
		return None
	if not isinstance(parsed, dict):
		return None

	intent = parsed.get("intent")
	if intent not in VALID_INTENTS:
		return None

	item_mention = parsed.get("item_mention")
	if item_mention is not None and not isinstance(item_mention, str):
		return None

	return {"intent": intent, "item_mention": item_mention}


def _classify_with_anthropic(text: str, api_key: str, model: str) -> dict | None:
	try:
		response = requests.post(
			"https://api.anthropic.com/v1/messages",
			headers={
				"x-api-key": api_key,
				"anthropic-version": "2023-06-01",
				"content-type": "application/json",
			},
			json={
				"model": model,
				"max_tokens": 200,
				"system": _SYSTEM_PROMPT,
				"messages": [{"role": "user", "content": text}],
			},
			timeout=_REQUEST_TIMEOUT_SECONDS,
		)
	except requests.RequestException as e:
		frappe.logger().error(f"Anthropic intent classification failed (request error): {e}")
		return None

	if response.status_code != 200:
		frappe.logger().error(f"Anthropic intent classification failed: HTTP {response.status_code} | {response.text[:500]}")
		return None

	try:
		body = response.json()
		raw_text = next(block["text"] for block in body["content"] if block.get("type") == "text")
	except (ValueError, KeyError, StopIteration):
		frappe.logger().error(f"Anthropic intent classification: unexpected response shape | {response.text[:500]}")
		return None

	return _parse_intent_response(raw_text)


def _classify_with_gemini(text: str, api_key: str, model: str) -> dict | None:
	try:
		response = requests.post(
			f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
			headers={"x-goog-api-key": api_key, "content-type": "application/json"},
			json={
				"systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
				"contents": [{"role": "user", "parts": [{"text": text}]}],
				"generationConfig": {"responseMimeType": "application/json"},
			},
			timeout=_REQUEST_TIMEOUT_SECONDS,
		)
	except requests.RequestException as e:
		frappe.logger().error(f"Gemini intent classification failed (request error): {e}")
		return None

	if response.status_code != 200:
		frappe.logger().error(f"Gemini intent classification failed: HTTP {response.status_code} | {response.text[:500]}")
		return None

	try:
		body = response.json()
		raw_text = body["candidates"][0]["content"]["parts"][0]["text"]
	except (ValueError, KeyError, IndexError):
		frappe.logger().error(f"Gemini intent classification: unexpected response shape | {response.text[:500]}")
		return None

	return _parse_intent_response(raw_text)


def classify_message(text: str) -> dict | None:
	"""Returns {"intent": ..., "item_mention": ...} or None. None covers every "can't
	trust this" case alike — provider not configured, unreachable, or a response we
	couldn't parse/validate — callers must treat it as "leave this message for a
	human", never as "no item mentioned"."""
	if not text or not text.strip():
		return None

	provider = frappe.conf.get("dms_erp_llm_provider")

	if provider == "anthropic":
		api_key = frappe.conf.get("dms_erp_anthropic_api_key")
		if not api_key:
			frappe.logger().warning("dms_erp_llm_provider is 'anthropic' but dms_erp_anthropic_api_key is not configured.")
			return None
		model = frappe.conf.get("dms_erp_anthropic_model") or "claude-haiku-4-5"
		return _classify_with_anthropic(text, api_key, model)

	if provider == "gemini":
		api_key = frappe.conf.get("dms_erp_gemini_api_key")
		if not api_key:
			frappe.logger().warning("dms_erp_llm_provider is 'gemini' but dms_erp_gemini_api_key is not configured.")
			return None
		model = frappe.conf.get("dms_erp_gemini_model") or "gemini-2.5-flash"
		return _classify_with_gemini(text, api_key, model)

	# Not configured at all -- every inbound message just waits for a human, same as
	# before this module existed. Not an error: a site with no LLM key set is a
	# perfectly valid, deliberate configuration.
	return None
