# Royalty Net & Accurate Net Sales — Implementation Spec (Toast → Snowflake handoff)

**Purpose:** everything needed to reproduce Newk's *Royalty Net* and *Accurate Net Sales
(historical)* logic in another stack (e.g., Snowflake + a separate reporting app).
This is source-system-agnostic: it describes the Toast data and the exact math. A working
reference implementation exists in this repo (`backend/main.py`) — see §7.

---

## 1. The three figures

| Figure | Definition | One-line |
|---|---|---|
| **Toast Net Sales** | Toast's `netSalesAmount` for a store × business date. | What Toast reports as net sales. |
| **Royalty Net** | `Toast Net Sales − restaurant-destination service charges` | Newk's royalty-bearing net. |
| **Accurate Net Sales (historical)** | `Toast Net Sales + specific deferred-item sales` | Makes pre- vs post-7/6/2026 comparable. |

**Why Royalty Net subtracts service charges:** Toast *includes* service charges whose
`destination = RESTAURANT` (delivery fees, catering fees, etc.) inside `netSalesAmount`.
Newk's pays those out (to delivery providers, etc.), so they are **not** royalty-bearing.
Charges to `SERVER`, `TOAST`, or `THIRD_PARTY` are **never** in `netSalesAmount` and must
**not** be subtracted.

**Why the deferred add-back exists:** Newk's currently marks certain fee line-items
(*Tech Fee*, *Delivery Fee (D)*, *Service Fee (D)*) as **deferred** in Toast, which
**excludes** them from `netSalesAmount`. **Effective 2026-07-06** accounting stops deferring
them → they post to net sales → Toast net **jumps**. To keep historical (pre-7/6)
comparisons valid, add those deferred sales back to pre-cutover Toast net. After 7/6 the
deferred amount is naturally ~$0 (items no longer deferred), so *Accurate Net Sales* is one
continuous, comparable series across the cutover.

---

## 2. Data sources (Toast)

| Need | Toast source | Key fields |
|---|---|---|
| Toast Net Sales | Enterprise Reporting / Analytics (ERA) `metrics` | `restaurantGuid`, `businessDate`, `netSalesAmount` |
| Service charges | **Orders API** (`ordersBulk` by businessDate) → order → check → `appliedServiceCharges` | `chargeAmount`, `destination`, `name`, `serviceCharge.guid` |
| Service-charge names/dest | **Config API** `/config/v2/serviceCharges` | `guid`, `name`, `destination`, `gratuity` |
| Deferred items | **Orders API** → order → check → `selections` (recurse `modifiers`) | `deferred`, `price`, `displayName`, `item.name`, `voided`, `quantity`, `tax` |

`destination` ∈ `{RESTAURANT, SERVER, TOAST, THIRD_PARTY}`.
In Snowflake, land the equivalent order/check/selection/appliedServiceCharge tables plus the
service-charge config, or use Toast's reported `netSalesAmount` if that's landed.

---

## 3. Computation — per store × business date

### 3a. Restaurant service charges (drives Royalty Net)
```
for each order (skip if order.voided):
  for each check (skip if check.voided or check.deleted):
    for each applied_sc in check.appliedServiceCharges:
        amt = applied_sc.chargeAmount
        if amt <= 0: continue
        dest = applied_sc.destination  (default 'RESTAURANT' if missing)
        if dest != 'RESTAURANT': continue          # only these are inside netSalesAmount
        name = applied_sc.name
               or serviceChargeConfig[applied_sc.serviceCharge.guid].name
               or 'Service Charge'
        sc_by_name[name] += amt
        sc_total         += amt

RoyaltyNet = ToastNetSales - sc_total
```
> `appliedServiceCharge.serviceCharge` is a **stub** (guid + entityType only) — resolve the
> human name from the applied charge's own `name`, else the config API.

### 3b. Deferred items (drives the historical add-back)
```
collect_deferred(selections):           # recurse into selection.modifiers
  for sel in selections (skip if sel.voided):
     if sel.deferred: emit sel
     collect_deferred(sel.modifiers)

for each deferred sel:
    sales = sel.price                    # post-discount, pre-tax
    if sales == 0: continue
    name = sel.displayName or sel.item.name or '(unnamed)'
    if name.strip().lower() not in ADDBACK_ALLOWLIST: continue
    deferred_by_name[name] += sales
    deferred_total          += sales

AccurateNetSales = ToastNetSales + deferred_total

# ADDBACK_ALLOWLIST = { 'tech fee', 'delivery fee (d)', 'service fee (d)' }
```
> **Add-back set (FINALIZED as an explicit allowlist):** `Tech Fee`, `Delivery Fee (D)`,
> `Service Fee (D)` — matched case-insensitively (trimmed) on `displayName`. An allowlist
> (not "all deferred minus tips") is deliberate: it guarantees gift-card, house-account,
> and other deferrals are **never** added into net sales. If a catering / 3rd-party fee
> variant appears under a different name, add it to the allowlist.

---

## 4. Prior Year (YoY)

1. **Map the PY date(s):**
   - `same_day` = current date − 364 days
   - `same_date` = current date − 365 days
   - `fiscal` = map each current day to its prior-year **fiscal-equivalent** day (Newk's runs a
     12-period fiscal calendar; not a fixed offset).
2. **If the store was on Toast on the PY date:** compute PY Toast Net, PY Royalty Net, PY
   service charges, PY deferred using the **exact same** logic (§3) on the PY date's data →
   PY source = **Toast**.
3. **If the store was on Aloha on the PY date** (pre-cutover): Toast has no data → fall back
   to the legacy Aloha net-sales figure. PY source = **Aloha**; Royalty Net / service-charge
   detail **not available** (Aloha service-charge methodology is still TBD).
4. **"Was it on Toast on the PY date?"** = does Toast return data for that store on that date.
   Cutover was staggered across 2024–2026 (last store, Lubbock #1188, ~May 2026), so a store
   being on Toast *today* does **not** mean it was a year ago — check per store × PY date.

---

## 5. Gotchas (the things that cost us time)

1. **Toast `netSalesAmount` already includes restaurant-destination service charges.** This is
   the whole reason Royalty Net exists. Subtract them; don't double count.
2. **Deferred items are NOT service charges.** Deferred fees are *line items*
   (`selection.deferred`), not `appliedServiceCharges`. A store can show **$0 service charges**
   yet a non-zero deferred add-back. Keep the two computations separate. (Real example:
   Jackson #1011 on 6/30/2026 had $0 restaurant SC but $119 of deferred fees.)
3. **Only `destination = RESTAURANT`** charges are in net sales. SERVER/TOAST/THIRD_PARTY are not.
4. **Recurse into `selection.modifiers`** when collecting deferred items.
5. **Exclude voided/deleted** orders, checks, and selections throughout.
6. **Tips/gratuity are excluded** from the add-back (they aren't sales).
7. **Fiscal-calendar YoY** maps by fiscal-equivalent day, not a fixed −365. A same-date offset
   quietly produces slightly different prior-year days.
8. **Cutover date 2026-07-06** for the deferred change; **staggered Toast go-live** across stores
   governs Toast-vs-Aloha PY sourcing.

---

## 6. Worked example (use to validate your build)

**Jackson, MS #1011 — business date 2026-06-30 (CY) vs 2025-07-01 (PY, same-day):**

| | Toast Net | Restaurant SC | Royalty Net | Deferred add-back |
|---|---|---|---|---|
| CY 6/30/2026 | 9,906.62 | 0.00 | **9,906.62** | 119.00 |
| PY 7/1/2025 (Toast) | 10,307.35 | 95.00 | **10,212.35** | 361.39 |

If your Snowflake logic reproduces these four numbers for this store/day, the core math is right.

---

## 7. Reference implementation (this repo)

Repo: `Newks-App` (FastAPI backend `backend/main.py` + `frontend/legacy.html`).

- `_compute_netsales_detail_for_day()` — §3a + §3b in one pass over the Orders API.
- `pivot_metrics_by_location_and_date()` — assembles `royaltyDetail` (per-location + chain).
- `_build_toast_netsales_pivot()` + `_merge_py_royalty_detail()` — §4 (PY-from-Toast, incl. fiscal).
- `_fetch_service_charge_configs()` — the Config API name/destination lookup.
- Toast net (`netSalesAmount`) comes from the ERA `metrics` report (`request_toast_report` /
  `fetch_toast_report`).

Independent cross-check that predates this app: Don Wilson's SQL proc
`sp_rpt_Toast_Net_Sales_Validation` establishes the same bridge —
`Toast Web Net = item net + service charges − refunds`.

---

## 8. What to send the VP (checklist)

- [ ] **This spec** (`ROYALTY_NET_HANDOFF_SPEC.md`).
- [ ] **Repo access** to `Newks-App` (the reference functions in §7).
- [ ] **Toast API scope**: ERA/Analytics `metrics`, Orders API, Config `/config/v2/serviceCharges`.
- [x] **The deferred un-defer allowlist (finalized):** `Tech Fee`, `Delivery Fee (D)`,
      `Service Fee (D)` — exact-name allowlist; add variants if new fee names appear.
- [ ] **The store Toast go-live dates** (for Toast-vs-Aloha PY sourcing).
- [ ] **The fiscal calendar** (current-day → prior-year fiscal-day mapping) if he wants fiscal YoY.
- [ ] **Validation targets** (§6, plus a couple more stores once confirmed).
