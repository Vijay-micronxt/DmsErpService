app_name = "dms_erp"
app_title = "Pacific DMS"
app_publisher = "MicroNXT"
app_description = "Pacific Inc internal operations backend: staff auth, catalog/pricing, warehouse, purchase, finance and comms — built on top of ERPNext."
app_email = "vijay@micronxt.com"
app_license = "Proprietary"

# required_apps intentionally left as just frappe for Phase 0 (auth). This app is meant
# to be installed on a site that already has erpnext installed (later phases use ERPNext's
# native Warehouse/Bin/Stock Ledger doctypes rather than a custom stock model), but we don't
# force that ordering here since Phase 0 doesn't touch any ERPNext doctype yet.
required_apps = ["frappe"]

# The internal staff app (React/TanStack SPA) talks to this app purely over whitelisted
# JSON API methods using JWT bearer tokens. We deliberately do not touch app_include_js,
# website_route_rules, or any Desk/website hook — no request from this app should ever
# redirect into /app.

before_request = ["dms_erp.auth.middleware.authenticate_request"]

after_install = "dms_erp.setup.install.after_install"
after_migrate = "dms_erp.setup.install.after_migrate"
