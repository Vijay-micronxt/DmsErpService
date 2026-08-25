import frappe

# Canned quick-reply templates (BRD §5) with {placeholder} slots the caller fills in
# client-side — same static list the frontend keeps, not a doctype, since nothing in
# this phase needs them editable without a deploy. Revisit if that changes.
MESSAGE_TEMPLATES = [
	{
		"label": "Item available",
		"text": "Good news — {item} is back in stock ({stock} boxes available). Let us know if you'd like to confirm the order.",
	},
	{
		"label": "Price update",
		"text": "Updated price for {item}: {price}/box, effective today. Happy to share a formal quotation.",
	},
	{
		"label": "Payment reminder",
		"text": "This is a reminder that your outstanding balance is {outstanding}. Please arrange payment at your earliest convenience.",
	},
	{"label": "Custom message", "text": ""},
]


def verify_webhook_secret(secret: str):
	"""Placeholder auth for the (future, external) WhatsApp middleware's webhook calls
	— a shared secret in site_config, same pattern as the JWT signing keys in Phase 0.
	The real middleware integration isn't scoped yet ("the real send/receive logic is
	middleware-side per the BRD"); swap this for whatever that integration actually
	uses (Meta's verify-token handshake, an HMAC signature, an IP allowlist) once it's
	built, rather than leaving these endpoints open to any anonymous caller."""
	configured = frappe.conf.get("dms_erp_whatsapp_webhook_secret")
	if not configured:
		frappe.throw("dms_erp_whatsapp_webhook_secret is not configured in site_config.json.")
	if not secret or secret != configured:
		frappe.throw("Invalid webhook secret.", frappe.PermissionError)
