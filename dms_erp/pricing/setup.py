import frappe

DEALER_PRICE_LIST = "Dealer"


def setup_pricing():
	if frappe.db.exists("Price List", DEALER_PRICE_LIST):
		return
	frappe.get_doc(
		{
			"doctype": "Price List",
			"price_list_name": DEALER_PRICE_LIST,
			"selling": 1,
			"currency": frappe.defaults.get_global_default("currency") or "INR",
			"enabled": 1,
		}
	).insert(ignore_permissions=True)
