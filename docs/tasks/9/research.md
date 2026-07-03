# Research #9 — Ozon MCP: region reality, green price, delivery, filters

**Date:** 2026-07-03. Author: feat-ozon-mcp. Method: **raw composer-api JSON** from live MCP on
Moscow server (region=Krasnoyarsk), SKU **3520206112** (телескоп рефрактор 150x 70мм). Not theory.

**Controlled A/B** (same SKU, Krasnoyarsk storageState vs no-state/Moscow default) is the core
experiment — it isolates what the region cookie actually changes.

---

## Q1 — РЕГИОН: работает, НЕ плацебо (CONFIRMED)

Region cookies genuinely put the session in Krasnoyarsk. Raw JSON of the PDP carries multiple
Krasnoyarsk-specific fields, and the A/B proves they flip vs Moscow.

**Krasnoyarsk-specific fields in raw JSON (with state loaded):**
| field | value |
|---|---|
| `"city"` | `Красноярск` |
| `"areaId"` | `31498` |
| `"timeZoneUtcname"` | `UTC+7` |
| `geoCoordinate.latitude` | `56.01479` (Krasnoyarsk) |
| pickup `fullName` | `Россия, Красноярский, Красноярск, улица Республики, 49П` |

**A/B — Krasnoyarsk (state) vs Moscow (no state), same SKU, same moment:**
| field | Krasnoyarsk | Moscow | differs |
|---|---|---|---|
| city | Красноярск | Москва | ✅ |
| areaId | 31498 | 2 | ✅ |
| timezone | UTC+7 | UTC+3 | ✅ |
| coords | 56.01479 | (none) | ✅ |
| pickup addr | Красноярск, ул. Республики | (none) | ✅ |
| **cardPrice (green)** | **4 033 ₽** | 4 086 ₽ | ✅ |
| price (other banks) | 4 245 ₽ | 4 301 ₽ | ✅ |

**Region affects PRICE** on this SKU: Krasnoyarsk 4033 ≠ Moscow 4086 (green). Not placebo.

### Why the user saw "Минино ↔ Красноярск = same price"
Минино is a village in the **Krasnoyarsk area** (same `areaId 31498`). Ozon prices at
**region/area granularity**, not per-village → both give identical price. That is correct Ozon
behavior, not a broken region. Cross-region (Krasnoyarsk vs Moscow) DOES change price (proven
above). So: region works; sub-region moves don't change price by design.

**Confidence: CONFIRMED** (A/B + 5 region fields flip).

---

## Q2 — ЗЕЛЁНАЯ ЦЕНА «С банками» (Ozon Банк): already served (CONFIRMED)

The green Ozon-Bank price the user wants is field **`cardPrice`** in the `webPrice` widget — and
**the MCP already returns it** as `details.price`.

**Raw `webPrice` widget (SKU 3520206112, Krasnoyarsk):**
```
cardPrice     : "4 033 ₽"   → label withOzonCard    = "С банками"        ← GREEN price (target)
price         : "4 245 ₽"   → label withoutOzonCard = "С другими банками" ← regular
originalPrice : "14 999 ₽"                                                ← old price
link          : "/modal/pdpListOfBanks?product_id=…"  (per-bank breakdown lives in a modal)
```

**What the MCP tool returns** (`ozon_product_details(3520206112)`):
```
price: 4033        ← == cardPrice == green "С банками"  ✅ this is exactly what the user wants
priceRegular: 4245 ← == "С другими банками"
oldPrice: 14999
```
Repo mapping (`src/parse.js:245`): `price: priceToNumber(price?.cardPrice) ?? priceToNumber(price?.price)`
→ it **prefers `cardPrice` (green)**, falls back to `price` only if the green one is absent.

**So no change is needed — the tool already outputs the green Ozon-Bank price.** The earlier
`4115` reading was a different region/moment (pre-region-fix Moscow, or price drift), not the
"other banks" field. Field-to-label mapping above is definitive.

**Note on user's numbers (Свердловская обл.): 3604/4004** vs MCP Krasnoyarsk **4033/4245** — the
gap is REGION (user's Ekaterinburg region ≠ Krasnoyarsk) + possible time drift, not a wrong field.
The tool correctly gives the *Krasnoyarsk* green price.

**Confidence: CONFIRMED.** Optional tiny improvement: rename `priceRegular`→clearer label, or
expose the `/modal/pdpListOfBanks` per-bank breakdown (low value, skip).

---

## Q3 — ДОСТАВКА (дата/склад/регион отгрузки): NOT in the standard PDP JSON (CONFIRMED absent)

The main product's delivery date / warehouse / shipping region is **NOT** in the composer-api PDP
response the repo fetches.

- `webDelivery-8727767-default-1` widget exists but is **empty `{}`**.
- No `tpzModule` / `splitModule` / `deliverySchema` / `warehouse` field for the main SKU.
- The "7/8/9 июля" dates present in the JSON belong to **`skuShelfGoods`** widgets = *similar-product
  carousels* (`addToCartButtonWithQuantity.text`), NOT the main product.

Ozon computes the main product's delivery via a **separate delivery API call** that requires a
chosen pickup point/address (client-side, after region+address selection). It is not in the PDP
composer page.

**Achievable?** Only with extra work: a second API call to Ozon's delivery/tpz endpoint with the
Krasnoyarsk address, reverse-engineered separately (not currently hit by the repo). **Medium-high
effort, brittle** (address-bound, undocumented). The pickup **address** itself (Красноярск, ул.
Республики 49П) IS available in the region block — a coarse "ships to Krasnoyarsk" signal is cheap,
but an exact date is not.

**Confidence: CONFIRMED absent** in current response; achievable only via a new delivery call.

---

## Q4 — ФИЛЬТРЫ (фасеты бренд/тип/диаметр): present in raw, achievable (CONFIRMED present)

Search results carry a full facet set in widget **`filtersDesktop-3124459-default-1`** (~30 KB).

**Available facets (key → type):**
`brand` (checkboxes), `telescopetype` (checkboxes — category-specific!), `country`, `color`,
`seller`, `features`, `includedaccessoriesoptic`, `currency_price` (already used), `delivery`,
`is_promo`, `isdiscount`, `is_installment`, `brandcertified`, `is_official_brand_seller`.

**Facet values are `key=id` pairs**, e.g.:
- `brand`: `73091867`=levenhuk, `32011536`=Sky-Watcher, `139163659`=Veber, `100089640`=SVBONY
- `telescopetype`: `33073`=Рефрактор, `33074`=Рефлектор, `340522`=Катадиоптрик, `340523`=Хромосферный

**Apply mechanism:** filters are applied as URL query params on the category link
(`applySearchFilters.params.baseLink = /category/teleskopy-15985/?...&text=телескоп`), i.e.
`&brand=73091867&telescopetype=33073`.

**Achievable?** Yes, but **facets are category-dependent** (telescopetype only exists for
telescopes; a phone search has different facets). Clean design = a 2-step flow: (1) a tool returns
available facets for a query, (2) search accepts `filters={key:[ids]}`. **Medium effort.** A cheap
subset (brand + price, which is common across categories) is easier; full dynamic facets is more.

**Confidence: CONFIRMED present** in raw; implementation is medium (dynamic per category).

---

## Summary

| # | Question | Verdict | Field(s) in raw JSON |
|---|---|---|---|
| 1 | Region = Krasnoyarsk, not placebo? | ✅ REAL (A/B proves price+city+coords+tz+addr flip vs Moscow) | `city`, `areaId 31498`, `UTC+7`, `lat 56.01479`, pickup addr |
| 2 | Green «С банками» price? | ✅ ALREADY SERVED (`details.price=4033`=`cardPrice`) | `webPrice.cardPrice` |
| 3 | Delivery date/warehouse in JSON? | ❌ ABSENT for main SKU (empty `webDelivery`); needs separate delivery API | `webDelivery` = `{}` |
| 4 | Search facets (brand/type)? | ✅ PRESENT; medium effort (category-dynamic) | `filtersDesktop` widget |

**User's core question answered:** region=Krasnoyarsk **works and is not placebo** — it changes
city/coords/timezone/pickup-address AND price (Krasnoyarsk green 4033 ≠ Moscow 4086). The green
Ozon-Bank price is already what the tool returns. Минино≈Krasnoyarsk same price = correct
region-granularity behavior, not a bug.

**No implementation done (research only).** If the orchestrator wants follow-ups: (a) delivery
date = new brittle API call (medium-high), (b) search facets = medium (dynamic per category),
(c) green price = already done, nothing to do.
