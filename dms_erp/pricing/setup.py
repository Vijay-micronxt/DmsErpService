import frappe

from dms_erp.pricing.dealer_classification import DEALER_CLASSIFICATIONS

DEALER_PRICE_LIST = "Dealer"

# BRD C.1.4/C.7.1: three price lists, one per dealer classification tier — the
# same names recompute_dealer_classifications() writes onto Customer, so a
# classification value is always also a valid Price List name with no mapping
# table needed. "Dealer" (the existing, pre-tiering price list) is kept as the
# live name for the middle tier rather than renamed, so every price already
# published there before tiering existed stays valid with no migration.
PRICE_LISTS = DEALER_CLASSIFICATIONS


def setup_pricing():
	for price_list in PRICE_LISTS:
		if frappe.db.exists("Price List", price_list):
			continue
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": price_list,
				"selling": 1,
				"currency": frappe.defaults.get_global_default("currency") or "INR",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
