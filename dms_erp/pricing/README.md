# Pricing

Phase 2: Product launch pricing. Implemented.

`api.py` + `doctype/item_price_proposal` — a custom doctype (no ERPNext equivalent
for a proposed landing-cost/margin breakdown awaiting approval, with an audit trail).
Once approved, the live price is published to the standard ERPNext `Item Price` (on
the "Dealer" selling Price List) — everything downstream reads the native object, not
a custom field.
