# Warehouse

Phase 3: Bay management, allocation, inward, transfers, stock. Implemented, entirely
on top of ERPNext's native stock doctypes — there is no custom stock ledger anywhere
in this module.

- **Bays** (`bay_api.py`) — ERPNext `Warehouse`, nested under the two physical
  warehouses ("Pacific Main — Morbi", "Pacific Buffer — Wankaner"). Custom Fields
  only for what Warehouse has no equivalent for: bay type, dimensions, capacity,
  suitable categories, zone/row, a 3-state bay status.
- **Stock / lots** (`stock_api.py`, `utils.list_stock_lots`) — a live aggregate over
  `Stock Ledger Entry` grouped by item+warehouse+batch. Not a doctype — Bin has no
  batch dimension, so batch-wise on-hand qty is read straight from the ledger. Each
  lot also carries `damageType`/`claimRef` (Phase 9) for lots sitting in a damage or
  insurance-claim bay — `damageType` is just that bay's `custom_bay_type`, and
  `claimRef` is traced back through the ledger to the Stock Entry that moved the lot
  there and its `custom_claim_ref` (Phase 6), the same trace the Phase 8 dashboard's
  "damage awaiting claim" count already did — now shared via `utils.claim_ref_for_lot`
  instead of duplicated. Both are `null` for lots outside a damage/claim bay.
- **Inward Truck** (`doctype/inward_truck`) — a genuinely custom doctype (gate/LR/
  ETA tracking has no ERPNext equivalent). Does not itself move stock.
- **Bay Allocation** (`allocation_api.py`, `doctype/bay_allocation`) — a custom
  doctype for the suggest/confirm/split-across-bays workflow (no ERPNext
  equivalent), but confirming one **posts and submits a native Purchase Receipt**
  (one item row per bay split) — that's the actual stock-effecting event. Scan-
  confirmed put-away (`confirm_putaway`, `resolve_scan`) happens after that and is a
  floor-confirmation gate only; it does not move stock again. `list_allocations`/
  `get_allocation` (Phase 9) are plain reads over the doctype — they were missing
  even though every write action already existed.
- **Transfers** (`transfer_api.py`) — a native `Stock Entry` (Material Transfer),
  not a custom doctype. Custom Fields only for transfer type/reason/damage-type/
  claim-ref, which Stock Entry has no equivalent for.

Linking a Purchase Receipt back to a Purchase Order (so PO received-qty tracks
correctly) is deliberately deferred to Phase 4, once real POs exist in this app —
`Inward Truck.purchase_order`/`purchase_order_item` are informational links only for
now.

`warehouseKpis`/`warehouseAlerts` (from the frontend's `warehouse-dashboard.ts`) are
NOT built here — they're pure dashboard aggregation, several of them depending on
Picking (Phase 5), so they belong in Phase 8 (Dashboard/Analytics).
