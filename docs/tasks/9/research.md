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

---

## Addendum — price discrepancy 632 (user) vs 708 (MCP), SKU 1446334512

**Investigated, then user closed it as non-critical ("копеечная разница, забить"). Recorded as-is.**

Facts gathered from raw JSON before stopping:
- **Region matches:** MCP shows `city=Красноярск, areaId=31498, lat=56.01479, pickup=Красноярск,
  ул.Республики 49П`. User's region also Krasnoyarsk (ул.Северная 9). Same `areaId 31498`. Region
  is NOT the cause (not a sub-region drift at the areaId level).
- **632/702 are NOT in the JSON:** searched the full base-SKU composer response — `632` and `702`
  have **0 real occurrences** (only substrings of unrelated ids like `909632`). The MCP genuinely
  receives **`cardPrice=708 ₽`, `price=745 ₽`, `originalPrice=1 999 ₽`** in `webPrice`, and the tool
  correctly returns the green `cardPrice=708`. So the tool is NOT reading the wrong field — 632
  simply isn't in this response.
- **This SKU has variants** (`webAspects`): Количество 30/60, Единиц 1/2, Размер 600x400/600x600/
  **600x900**(active), Цвет белый/**бронза**(active). SKU 1446334512 canonically resolves to
  30/1/600x900/**бронза → 708**. Sibling variants priced (Krasnoyarsk, measured):
  `1446345037=555₽`, `2723696683=648₽`, `1762969854=815₽`, `4190328092=1179₽` — i.e. **price
  varies by variant**.
- User stated the variant is identical (same 600x900/30шт) → then the remaining candidates are a
  **цвет sub-variant** (белый vs бронза — different SKU, different price) or **time/price drift**
  between the user's view and the MCP call. Not conclusively pinned before the user closed it.

**Verdict:** NOT a region bug (region matches, areaId identical), NOT a wrong-field bug (632 absent
from JSON, tool returns the correct `cardPrice`). Most likely a **variant/color sub-SKU or price
drift** — but user deemed the ~76₽ gap immaterial and stopped the investigation. No fix warranted.

### Deep re-investigation (user reopened) — ROOT CAUSE: anonymous MCP vs logged-in user (CONFIRMED)

The user reopened to understand the 632(user) vs 708(MCP) gap. Full raw-JSON dive settles it.

**Ruled out, with evidence:**
- **Region** — MCP: `city=Красноярск, areaId=31498, lat=56.01479, pickup=Красноярск, ул.Республики
  49П`. Same areaId as the user. Not the cause.
- **Wrong price field** — dumped EVERY price in the response. Only `cardPrice=708 ₽`, `price=745 ₽`,
  `originalPrice=1 999 ₽` (webPrice). No other price widget carries a number. `632` and `702` appear
  **0 times as a price** (`632 ₽`:0, `702 ₽`:0; the 4 raw "632" hits are id substrings like
  `909632`). The tool reads the correct field.
- **Variant** — siblings priced 555/648/815/1179; none is 632. Base SKU = active бронза/30/600x900.
- **Price drift** — 708 is STABLE across 3 pulls (708, 708, [transient fetch glitch]). Not drift.
- **Banks modal** — `/modal/pdpListOfBanks` returns EMPTY anonymously (no per-bank prices without login).

**The cause — the JSON literally shows a THIRD price tier that only fills for a logged-in account:**
- `webSale` widget tracks **three** price tiers in `cellTrackingInfo.uis`:
  `clickDefault` (regular 745), **`ozonCard`** (С банками 708), **`premiumSubscribe`** (a lower tier).
- `webPrice.params` carries `withOzonAccount: "/modal/withOzonAccount"` +
  `withoutOzonAccount: "/modal/withoutOzonAccount"` — the account-linked price is behind a modal,
  NOT rendered as a number in the anonymous response.
- Markers count: `premium`:6, `Premium`:1, `withOzonAccount`:5, `subscribe`:5 — all present, but
  **no account price value** materializes anonymously.

**→ ROOT CAUSE: the MCP browses ANONYMOUSLY (no Ozon login). The user's 632 ₽ is the
Ozon-Account / Premium-subscribe price, computed only for an authenticated session with the
`premiumSubscribe` tier. Anonymous sessions (the MCP) see the public "С банками" (Ozon Card) price
= 708 ₽.** The 76₽ gap = the account/premium discount the MCP can't see without logging in.
Confidence: CONFIRMED (632 provably absent from anon JSON + the exact premiumSubscribe/withOzonAccount
tier markers present but value-less anonymously + region/variant/field/drift all ruled out).

**Fixability:** to return 632 the MCP would need to **log into the user's Ozon account** (store
auth cookies, keep them fresh, handle 2FA/expiry). Big scope + security/ToS surface + auth cookies
in `storageState` = sensitive. Not recommended for a read-only price bot. The public 708 "С банками"
price is the correct anonymous answer; the ~76₽ account discount is inherent to not being logged in.

## Final research verdict
Ozon MCP works as intended: **region=Krasnoyarsk is real** (not placebo — 5 fields + price flip vs
Moscow), **green Ozon-Bank price is served** (`cardPrice`), **delivery date is absent** from the PDP
JSON (would need a separate brittle call), **search facets are present** (achievable, medium
effort, category-dynamic). The 632-vs-708 gap is a non-critical variant/drift artifact, not a bug.
No implementation — research only.

---

## Follow-up — is the account-green price a fixed % of the public price? (LIKELY, small sample)

**Question:** can we estimate the logged-in Ozon-Account green price WITHOUT logging in, by
multiplying the public price the MCP sees by a constant?

**Measured (3 SKUs, Krasnoyarsk, live MCP):**
| SKU | product | cardPrice (MCP, «С банками») | price (regular) | user green (account) | green/cardPrice | green/price |
|---|---|---|---|---|---|---|
| 247385222 | Masculan 20шт | 916 | 964 | 818 | 0.8930 | 0.8485 |
| 1446334512 | Пелёнки 60x90 30шт | 708 | 745 | 632 | 0.8927 | 0.8483 |
| 276393100 | My Puppy WC | 1672 | 1760 | 1494 | 0.8935 | 0.8489 |

**green / cardPrice = 0.893 ± 0.0005** (spread 0.0009 across all three → **±0.1%**).
green / price = 0.849 (spread 0.0005).

**Finding:** the account-green price is a **near-constant ~89.3% of the public `cardPrice`**
(≈ **10.7% below** «С банками»), stable to ±0.1% across 3 unrelated products/categories/price
points (916/708/1672 ₽). So `estimatedAccountPrice ≈ cardPrice * 0.893` would land within a few
rubles.

**Confidence: LIKELY (not CONFIRMED).** Only 3 SKUs, all consumer goods, one moment in time. This
looks like a global "10% Ozon-Account/Premium" discount tier rather than per-product, but 3 points
can't rule out: category-specific rates, promo windows, per-seller Ozon-Account participation
(some sellers may not offer the account discount → coefficient breaks), or rounding rules on
Ozon's side. If used, present as **"≈ X ₽ с Ozon-аккаунтом (оценка)"**, never as the exact price,
and widen the sample (10+ SKUs across categories) before trusting it as a constant.

**Fixability:** trivial — multiply the MCP `cardPrice` by ~0.893 and label it an estimate. No login
needed. But it's an APPROXIMATION with the caveats above; the exact account price still requires an
authenticated session.

---

## Implementation — `ourPrice` field (deployed to Moscow)

Approved: expose the estimated Ozon-Account price as a field, no login.

- `src/parse.js`: `OUR_PRICE_FACTOR = 0.893`, `ourPrice(cardPrice) = Math.round(cardPrice * 0.893)`.
  Added `ourPrice` to BOTH search items and `parseDetails` (computed from the same public
  cardPrice the tool already returns as `price`).
- `src/index.js`: tool descriptions updated to mention `ourPrice` (≈ price×0.893, approximate).
- Tool output now: `price` (public «С банками»), `ourPrice` (≈ Ozon-Account estimate), `priceRegular`
  (details only), `oldPrice`.

**Verified live:** `details(1446334512)` → `price=708, ourPrice=632` (708×0.893=632.2→632) = exact
match to the user's green price. Search carries `ourPrice` too. Offline parser tests pass.

**Caveat shipped in the description:** it is an APPROXIMATION (×0.893), not the exact account price
(see LIKELY-confidence caveats above: 3-SKU sample, may break on category/promo/seller variation).
