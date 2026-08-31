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

after_install = "dms_erp.setup.install.after_install"
after_migrate = "dms_erp.setup.install.after_migrate"
