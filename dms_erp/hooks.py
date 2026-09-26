app_name = "dms_erp"
app_title = "DMS"
app_publisher = "MicroNXT"
app_description = "Distributor internal operations backend: staff auth, catalog/pricing, warehouse, purchase, finance and comms — built on top of ERPNext."
app_email = "vijay@micronxt.com"
app_license = "Proprietary"

# erpnext is required from Phase 2 onward: catalog/pricing build on ERPNext's native
# Item, Item Group, Item Price, Price List and Item Alternative doctypes rather than
# reinventing a product/pricing model, and later phases lean on Warehouse/Bin/Stock
# Ledger the same way.
required_apps = ["frappe", "erpnext"]

# The internal staff app (React/TanStack SPA) talks to this app purely over whitelisted
# JSON API methods using JWT bearer tokens. We deliberately do not touch app_include_js,
# website_route_rules, or any Desk/website hook — no request from this app should ever
# redirect into /app.

before_request = ["dms_erp.auth.middleware.authenticate_request"]

# whats91's "Dealer Portal" Flow already has these bare (dot-free) names baked into
# each action.api_call node's endpoint_url (/api/method/get_item_info, not
# /api/method/dms_erp.comms.flow_api.get_item_info) -- aliasing them here means every
# new comms.flow_api endpoint works without re-pointing the Flow's own config. See
# comms/flow_api.py's own docstring for the full picture of what calls these and why.
override_whitelisted_methods = {
	"get_item_info": "dms_erp.comms.flow_api.get_item_info",
	"get_order_status": "dms_erp.comms.flow_api.get_order_status",
	"get_delivery_status": "dms_erp.comms.flow_api.get_delivery_status",
	"get_outstanding_due": "dms_erp.comms.flow_api.get_outstanding_due",
	"get_recent_orders": "dms_erp.comms.flow_api.get_recent_orders",
	"create_dealer_opportunity": "dms_erp.comms.flow_api.create_dealer_opportunity",
}

after_install = "dms_erp.setup.install.after_install"
after_migrate = "dms_erp.setup.install.after_migrate"

# BRD C.4.1: nobody is told a draft reorder plan needs review -- a daily digest to
# the purchase team is the smallest fix that closes that gap (see reorder_api.py).
# BRD C.1.4: dealer price-tier classification is recomputed nightly from confirmed
# Sales Order value, not maintained by hand (see pricing/dealer_classification.py).
# BRD C.10.5: opted-in Series get their items' discontinuation status advanced
# automatically once idle/store/sales signals all breach that Series' thresholds
# (see catalog/withdrawal_api.py) -- a no-op for every Series that hasn't opted in.
# BRD C.10.2: every Active Display Placement Slip gets a WhatsApp nudge once it
# crosses the next 3/6/12-month monitoring interval (see catalog/sample_api.py).
scheduler_events = {
	"daily": [
		"dms_erp.purchase.reorder_api.notify_reorder_review",
		"dms_erp.pricing.dealer_classification.recompute_dealer_classifications",
		"dms_erp.catalog.withdrawal_api.evaluate_product_withdrawals",
		"dms_erp.catalog.sample_api.send_display_monitoring_reminders",
	],
}
