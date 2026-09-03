# Report — Task #7: Ozon MCP + Krasnoyarsk region + wire to Kesha

**Date:** 2026-07-03. **Status:** ✅ DONE — all 6 tickets, deployed & verified end-to-end.

## What shipped
Kesha (Telegram bot on Contabo/France) can now search Ozon, read product cards and reviews —
with **Krasnoyarsk prices** — via a new `ozon` MCP server hosted on the user's **Moscow** server
(72.56.235.40), because Ozon hard-blocks Contabo's French IP. Kesha reaches it over SSH-stdio.

## Tickets
- **T1** ✅ Installed `eduard256/ozon-mcp-server` at Moscow `/opt/ozon-mcp-server` under a dedicated
  `ozon` user; npm ci + Chromium; live Ozon data confirmed (182KB JSON, 13s); parser tests pass.
- **T2** ✅ Krasnoyarsk region forced. Automated the map picker ("Способ доставки" → "Определить
  местоположение" with geolocation set POST-challenge → click a Krasnoyarsk pickup point →
  "Заберу отсюда"), saved cookies to `krsk-state.json`. Proven: distinct Krasnoyarsk assortment
  vs Moscow; antibot still passes with state; region cookies TTL 365d.
- **T3** ✅ SSH-stdio wiring: restricted `ozon` user, forced-command wrapper, pristine JSON-RPC
  (first byte is the response), 3 tools listed. Found+fixed an orphan-Chromium leak on disconnect.
- **T4** ✅ RAM kill-test PASSED: cap on `user-1002.slice` (MemoryMax=800M). Kernel OOM
  (`CONSTRAINT_MEMCG`) killed only the ozon slice; cryptobot/seedon-api/nginx survived. Peak real
  request ~306MB. Idle-close frees RAM after 10 min.
- **T5** ✅ Added `ozon` to Contabo `.mcp.json` (bare `ssh -T`, forced command supplies launch);
  4 existing servers byte-identical; one bot restart; logs show all 6 MCP loaded, no Conflict.
- **T6** ✅ End-to-end from the live bot path (kesha@Contabo → SSH → Moscow → Ozon): `ozon_search`
  "наушники" returned Krasnoyarsk-priced results. Docs updated.

## Files changed
**On Moscow 72.56.235.40** (deployed; refs saved in `docs/tasks/7/deployed/`):
- `src/browser.js` (+3 edits): load `krsk-state.json` via absolute path (`import.meta.url`);
  fail-closed region self-check (region ≠ Krasnoyarsk → throw → tool `isError`).
- `src/index.js` (+1 edit): `process.stdin.on("end"/"close", cleanup)` — no orphan Chromium on
  SSH disconnect.
- `ozon-mcp-wrapper.sh` (new): forced-command wrapper, direct `node` (clean stdio + lifecycle).
- `krsk-state.json` (new, gitignored ops file): Krasnoyarsk region cookies.
- `capture-region.mjs` (new): one-shot region capture / refresh tool.
- systemd: `user-1002.slice` drop-in `MemoryMax=800M, MemorySwapMax=0`; linger enabled for `ozon`.
- `/home/ozon/.ssh/authorized_keys`: kesha@Contabo key + forced command + no-pty/forwarding.

**On Contabo 158.220.127.161:**
- `/opt/cog-second-brain/.mcp.json`: +`ozon` entry (backup taken; diff = +ozon only).

**In this repo:** `CLAUDE.md` (+Ozon MCP section), `CHANGELOG.md` (v2.5.0), `docs/tasks/7/*`.

## What the tools return (raw, Krasnoyarsk region)
- **ozon_search** (`кофе`, limit 3): fields `sku, name, price, oldPrice, discount, rating,
  reviews, brand, url, image`. E.g. `2184₽ Tasty Coffee Бразилия Серрадо 1кг, rating 4.9, sku 643726547`.
- **ozon_product_details** (sku): `sku, name, url, price (2184), priceRegular (2299), oldPrice
  (3817), available, rating, reviews (155471), seller {name,rating,url}, images[11],
  characteristics {…}, description {text,…}`.
- **ozon_product_reviews** (sku, limit 3): `rating, totalReviews, count, reviews[{author, score,
  comment, pros, cons, date, useful, purchased, hasPhotos}]`.

## Limitations
- No price history. Reviews capped (1–30). First call ~13s (antibot), then 0.3–1s.
- Region held by `krsk-state.json` (365d cookies); on revert → tool errors (no silent Moscow
  fallback). Refresh: `node capture-region.mjs headless` on Moscow.
- Data from Ozon's internal composer-api — may change.

## Tests / verification
- Live Ozon data on Moscow (repo code): ✅ 182KB, region=Красноярск.
- Region flip proven (distinct assortment + region marker), antibot OK with state.
- RAM kill-test: OOM scoped to ozon slice, prod survived (kernel log).
- SSH handshake + all 3 tools over SSH from kesha@Contabo; orphan-free on disconnect.
- Bot post-restart: 6 MCP loaded, no Conflict; e2e `ozon_search` returns Krasnoyarsk prices.

## Breaking
None. 5 existing MCP untouched; existing bot behavior unchanged.

## Codex review
6 blocking findings — all accepted & fixed pre-deploy (MemoryMax→slice cap, single SSH contract,
pristine stdout, absolute storageState path, fail-closed region, orphan cleanup). See
`codex-review-plan.md`.

## 📝 RULE proposal
When deploying an SSH-launched stdio MCP → the memory cap must bind the **user slice** (or a
`--pipe` service), NOT a systemd unit (SSH processes land in `session-*.scope`, not the unit) —
and the MCP must exit on stdin EOF (`process.stdin.on("end")`) or Chromium orphans accumulate.
