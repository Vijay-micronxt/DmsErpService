# WhatsApp Dealer Portal Flow — Testing TODO

Personal checklist for exercising everything built this session against the real WhatsApp
Flow, before considering BRD C.2.2's automated menu production-ready. Check items off as
you go; the step-by-step click-path for most of these is also in the in-app help panel —
`pacific-tileflow`'s Communications screen → "Testing this flow (QA)" section.

Status: nothing below has been verified end-to-end yet except the one partial test noted.

---

## 1. Item Availability & Price ("1. Availability & Price")

- [ ] **In-stock reply** — real item code/name for an item in the test dealer's Dealer
      Catalog with real stock. Confirm size/finish, stock count, and price line.
- [ ] **Price hidden correctly** — same item, dealer with `custom_price_visible` off.
      Confirm no `₹` line appears at all (not a placeholder).
- [x] **Out-of-stock, basic reply** — confirmed: `Royal Glassy (AAS-001) is currently out
      of stock.` came through correctly.
- [ ] **Out-of-stock with lead time + alternative** — item with `Supplier lead time (days)`
      set and a real `Item Alternative` link (catalog-visible + in-stock alternative).
      Confirm both the lead-time line and "You may also consider…" line appear.
      (Blocked earlier by AAS-001's `Variant Of` field — see Products help panel's
      "Setting an alternative item" section for the fix; re-test once cleared.)
- [ ] **Batch-specific reply, single batch** — send a code/name plus a quantity an item's
      largest batch alone covers (e.g. `"105107 20 boxes"`). Confirm the reply names that
      one batch, not a flat total.
- [ ] **Batch-specific reply, combination** — same, but with a quantity no single batch
      covers, for an item split across 2-3 batches. Confirm the reply lists the batch
      combination and the combined total.
- [ ] **Shortfall reply** — quantity larger than every batch combined. Confirm it reports
      the shortfall instead of a broken/empty reply.
- [ ] **No quantity given still works** — plain code/name with no quantity, on an item
      with multiple batches. Confirm it's still the flat total (unchanged behavior).
- [ ] **Multi-item message** — several item codes/names in one message, comma/"and"/Hindi
      "और"-separated (e.g. `"RUSTIC-GREY, Royal Glossy"`). Confirm one reply line per item.
- [ ] **Fuzzy/typo name match** — a slightly misspelled item name with no exact code.
      Confirm it still resolves via the fuzzy fallback.
- [ ] **Hindi/Hinglish sentence** — item name embedded in a full Hindi/Hinglish sentence.
      Confirm it still resolves.
- [ ] **Inquiry side effect** — after any of the above, confirm a new Inquiry (source
      WhatsApp) actually appears in the Inquiries list, not just that the reply text
      looked right.

## 2. Delivery Status ("2. Delivery Status")

- [ ] Real Sales Order number belonging to the test dealer — confirm correct stage/date.
- [ ] A different dealer's order number — confirm it comes back "not found", never reveals
      the other dealer's order or its stage.

## 3. Order Status ("3. Order Status")

- [ ] Same two cases as Delivery Status above (own order vs. cross-dealer rejection).

## 4. Payment Due ("4. Payment Due")

- [ ] Dealer with a real outstanding balance — confirm the amount shown is correct.
- [ ] Dealer with zero outstanding — confirm the "no outstanding dues" wording.

## 5. Recent 5 Orders ("5. Recent 5 Orders")

- [ ] Dealer with several orders — confirm the 5 most recent show with correct stages.
- [ ] Dealer with zero orders — confirm the "no orders yet" wording.

## 6. Request More Info / Place Order (after an availability check)

- [ ] **Request More Info** — confirm it raises an Inquiry with the dealer's own typed
      note attached.
- [ ] **Place Order, end to end** — pick an in-stock item, choose a quantity band, enter a
      PO number when asked. Confirm:
  - [ ] A real Sales Order appears in Orders carrying that PO number.
  - [ ] The WhatsApp reply names the new order number.
  - [ ] The correct item carried through (not the button label — this was a real
        production bug, now fixed; don't skip re-verifying it).

---

## Setup gotchas to remember while testing

- Test dealer's WhatsApp number must match `Customer.custom_phone` exactly, or nothing
  resolves at all.
- An item must be in the dealer's **Dealer Catalog** (or have an **Item Dealer Code** for
  that dealer) to resolve at all — separate from being *sellable* in general.
- An **Alternative Item** you're trying to set may fail with *"Not allow to set
  alternative item for the item variants"* — that means ERPNext's own `Variant Of` field
  is set on it (this app never uses ERPNext's native variant system). Clear it under
  `/app/item/<code>` → Variants section first.
- If nothing arrives in Communications after a real WhatsApp message, `/app/whats91-webhook-log`
  won't help (that log is only for the separate free-text path) — check `/app/error-log`
  for a failed call, then check the Flow's own setup on whats91's platform if there's
  nothing there either.
