# Pricing

Phase 2: Product launch pricing. Implemented.

`get_price_for_dealer` (BRD C.1.4) normally keys off a dealer's automatic
Standard/Dealer/Master classification tier. `Customer.default_price_list`
(ERPNext's own native field, otherwise unused in this app) is a manual
override that **always wins** over the tier when staff set it, via
`sales.dealer_api.create_dealer`/`update_dealer`'s `priceList` field — for a
dealer with a genuine reason to bill off a specific list (export, a project
rate, any other selling Price List on the site) rather than their volume
tier. Still falls back to the flat "Dealer" list if the tagged list has no
rate for an item yet, same safety net the classification path already has.
The nightly classification recompute keeps running regardless — it just
has no effect on pricing for a tagged dealer.

`api.py` + `doctype/item_price_proposal` — a custom doctype (no ERPNext equivalent
for a proposed landing-cost/margin breakdown awaiting approval, with an audit trail).
Once approved, the live price is published to the standard ERPNext `Item Price` (on
the "Dealer" selling Price List) — everything downstream reads the native object, not
a custom field.

`save_cost_inputs` creates the proposal if the item has none yet. Proposals were only
ever seeded by `catalog.api.create_product`, so every Tally-migrated item (all of them,
on a migrated site) has live Item Prices but no proposal — and the pricing screen,
which now lists every item rather than only those with a proposal, had no way to start
one. The read side for those items' real rates is `catalog.api.get_item_prices`.

`suggest_price_from_history` (ISS-2026-00623) — a purchase-cost + margin %
*suggestion* for a new Item Price Proposal, derived from that item's own
historical Purchase Invoice/Sales Invoice lines (Tally migration; this app posts
neither doctype itself, live, so every line in them is real historical data, not
something the app's own workflow could have polluted). Median, not mean — a live
check against the real migrated site turned up genuine outliers (a non-tile
ledger-adjustment code, a couple of discontinued items cleared below cost) that
would skew an average. Deliberately one blended margin, not per-tier
(Standard/Dealer/Master, which is what ISS-2026-00623 actually asks for): the
same live check found every migrated Customer classified "Standard Dealer" —
`pricing.dealer_classification.recompute_dealer_classifications` has never
actually differentiated a dealer into "Dealer"/"Master Dealer" yet, so there's no
tier-tagged sales history to derive three separate margins from. Returns `None`
(never a guessed number) for an item with fewer than 2 real data points on either
side — most commonly a brand-new item with no Tally history at all.
