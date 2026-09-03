# Research — Task #7: Ozon MCP server on Contabo + Krasnoyarsk geolocation

**Date:** 2026-07-03
**Author:** feat-ozon-mcp (full-cycle worker)
**Status:** Phase 1 complete — blocker found on Contabo, **SOLVED via Moscow RU server** (see Phase 1B).

> **TL;DR:** Contabo (France) is hard-blocked by Ozon. The user's Moscow server
> (72.56.235.40, Timeweb) **passes Ozon's antibot** — verified live with the repo's
> real code (✓ 180KB JSON in 12.9s). Decision = **run Ozon MCP on the Moscow server**
> (it IS the Russian exit — no proxy needed), Kesha on Contabo calls it over **SSH-stdio**.
> Region=Krasnoyarsk is coordinate/addressbook-cookie driven — solvable, needs an
> implementation spike (details in Phase 1B finding R).

## Question
Can we run `eduard256/ozon-mcp-server` (Node + Playwright/Chromium) on Contabo
(158.220.127.161, France) and connect it to Kesha, with prices forced to **Krasnoyarsk**?

Priority questions from the task:
1. Is Ozon reachable from Contabo's French IP, or does the antibot block it? ← #1 risk
2. Is the repo alive/working?
3. How to force Krasnoyarsk region?
4. RAM budget?

---

## Finding #1 — ⛔ BLOCKER: Ozon hard-blocks Contabo's French datacenter IP

**Confidence: CONFIRMED** (live measurement, 2 runs + block-page text + 2nd source).

Ozon uses the **Variti** anti-bot. From Contabo (France, `158.220.127.161`) even a
correctly-configured **real headless Chromium** does NOT get through — it is blocked
at the **IP/geo layer**, not the browser-fingerprint layer.

### Evidence (live, on Contabo)
Installed Playwright 1.60.0 + Chromium on Contabo, ran two probe scripts:

**Probe 1** (repo-style config, 12s challenge wait):
```
title: "Похоже, нет соединения"          ← Ozon block page, not the real homepage
composer search HTTP: 403
  body: {"incidentId": "fab_nmk_20260703072409_...", "supportURL": ".../complaint/support..."}
region: {"status":403}
/geo/krasnoyarsk/ nav: HTTP 403
cookies set: only __Secure-ETC (NO passed-challenge token)
```

**Probe 2** (hardened: webdriver-masked, `timezoneId: Asia/Krasnoyarsk`, `locale ru-RU`,
`waitUntil: networkidle`, 60s + 30s extra polling):
```
title after 30s: still "Похоже, нет соединения", stuck on url ?__rr=1
BODY: "Похоже, нет соединения
       Выключите VPN, перезагрузите роутер или подключитесь к другой сети
       Инцидент: fab_nmk_20260703072526_..."
cookies: __Secure-ETC, abt_data   ← antibot JS ran (abt_data present) but IP still blocked
composer search: HTTP 403
```

The block page text is the smoking gun:
> **«Похоже, нет соединения. Выключите VPN, перезагрузите роутер или подключитесь к другой сети»**
> ("Looks like no connection. Turn off VPN, reboot the router or connect to another network")

That is Ozon's message for **foreign / datacenter / VPN IPs**. The `fab_nmk` incident
prefix = Variti "fabric" block. The antibot challenge *completes* (`abt_data` cookie is
set — Variti's JS ran fine), then Variti **rejects the request because of the origin IP**.
This is not fixable by tuning the browser.

### Cross-check (2nd source)
- smart-lab.ru blog "how ozon.ru looks from abroad": ozon.ru is geo-blocked from outside
  Russia without a VPN — shows exactly this kind of error page. [1]
- Ozon has no public buyer API; the storefront is closed by Variti (repo README confirms). [2]

### What this means
The repo works **from a Russian IP** (that's what its README/Docker assume — run it where
your client is, in RU). It cannot work from Contabo (FR) as-is. **Contabo → Ozon needs a
Russian-exit egress.**

### Is there a Russian egress on Contabo already? — NO
- `env | grep proxy` → nothing.
- Only VPN service on the box is `xray.service`, but it's an **inbound VLESS server**
  (Contabo is a VPN *entry* node; its outbound = `freedom`/direct via Contabo's own FR IP).
  It does **not** give a Russian exit. Routing Ozon through it = still France = still blocked.
- Project proxy infra was deliberately removed ("прокси выпилен"). There is no RU proxy.

---

## Finding #2 — Repo is alive and well-built

**Confidence: CONFIRMED** (read all source).

`eduard256/ozon-mcp-server` — cloned latest. Structure (task assumed `index.js`, actually `src/`):
```
src/index.js   — MCP stdio server, 3 tools (ozon_search / _product_details / _product_reviews)
src/browser.js — single long-lived headless Chromium, passes Variti once, fetches composer-api JSON
src/ozon.js    — builds composer-api paths, parses
src/parse.js   — pure JSON parsers (widgetStates), offline-testable
test/, samples/ — offline parser tests
Dockerfile     — published image eduard256/ozon-mcp-server:latest
```
- Deps: `@modelcontextprotocol/sdk ^1.6.1`, `playwright 1.60.0` (pinned), `zod ^3.25`. Node ≥20 (Contabo has v20.20.2 ✓).
- Entry for `.mcp.json`: **`src/index.js`**, not `index.js`.
- Design is solid for our RAM concern: **10-min idle → browser auto-closes** (frees RAM);
  lazy launch on first call; relaunch on 403/307/crash. Blocks images/fonts? **No** — it must
  NOT block them (Variti loads its scripts through them; blocking → 403).
- Data comes from Ozon's internal `composer-api.bx/page/json/v2` — structured JSON, no HTML scraping.

**Caveat:** the whole design hinges on passing Variti, which (finding #1) fails from FR.

---

## Finding #3 — Krasnoyarsk region mechanism (moot until #1 solved, but researched)

**Confidence: LIKELY** (couldn't validate live — blocked before region-setting is reachable).

Ozon region is **cookie-driven**, not a URL param you can freely set:
- Region is stored in server-issued cookies set after the user picks a city in the UI
  (or navigates `https://www.ozon.ru/geo/krasnoyarsk/`). You capture them via
  `context.cookies()` / `storageState` and reuse. [1][2]
- An undocumented `select_location=<GUID>` composer param exists (Moscow/SPb/Novosibirsk
  GUIDs are known from a Habr Q&A), but responders report it "doesn't work alone" — needs
  an accompanying token/cookie. Not reliable on its own. [3]

**Planned approach (once IP is solved):** after the antibot page loads, drive the region
selection once (navigate `/geo/krasnoyarsk/` or click the city picker → choose Красноярск),
persist the resulting cookies to `storageState`, and load that state into the long-lived
context so every composer call carries the Krasnoyarsk region. Verify by reading the
`cityName`/region field back from a composer response. **This is a small, contained add to
`src/browser.js`.**

---

## Finding #4 — RAM budget: fine

**Confidence: CONFIRMED** (measured free RAM; known Chromium footprint).

- Contabo: `free -m` → **total 7941 MB, available ~6168 MB**, only ~1.7 GB used. (Not 8GB→6GB
  tight as feared; plenty of headroom.)
- One headless Chromium (Playwright) ≈ 200–400 MB resident under load; idle less. With the
  repo's 10-min idle-close, it's 0 MB when unused.
- Parallel colleague `feat-rag-upgrade` adds e5-large (~561 MB) to the same box → still
  comfortably within ~6 GB free. **RAM is not a blocker.**

---

## Bottom line / recommendation

**The task as written cannot ship: Contabo (France) is IP-blocked by Ozon (CONFIRMED live).**
The repo and RAM are fine; geolocation is solvable — but all of it is behind the IP wall.

### Options (need a decision before Phase 2)
- **A. Russian-exit egress for the Ozon MCP only.** Add an RU proxy/VPN egress that just this
  Chromium uses (`chromium.launch({ proxy })` or a SOCKS on the box). Needs a RU IP source
  (residential ideally — datacenter RU IPs are often blocked too). We currently have none.
  *Most faithful to the task; requires provisioning a RU egress.*
- **B. Run the Ozon MCP where the bot's users are (a RU box / the user's own network),** and
  bridge it to Kesha over stdio/pipe or a small socket. Contabo stays the bot host, Ozon MCP
  lives on a RU-reachable node.
- **C. Drop Ozon MCP** if no RU egress is acceptable.

**My recommendation: A** (dedicated RU proxy for the Ozon Chromium) is the cleanest — it keeps
everything on Contabo, one config line in `browser.js`. But it depends on getting a working
**Russian residential/mobile** proxy; a plain RU datacenter IP may also be Variti-blocked and
must be tested before committing. I did not find any existing RU egress in our infra.

**I need your decision on A / B / C (and, if A, where the RU proxy comes from) before I plan.**

---

## Sources
- [1] smart-lab.ru — "Полюбуйтесь, как выглядит ozon.ru из-за границы" — https://smart-lab.ru/blog/1293444.php (ozon.ru geo-blocked from abroad)
- [2] eduard256/ozon-mcp-server README — https://github.com/eduard256/ozon-mcp-server (Variti antibot, composer-api, run where client is)
- [3] Habr Q&A "API Ozon как получить данные с учетом гео?" — https://qna.habr.com/q/1311218 (select_location GUID param, needs token)
- Live probes on Contabo 2026-07-03 (raw output in this doc, findings #1)
```

## Raw probe artifacts
Probe scripts live at `/tmp/oztest/oz-probe.mjs` and `oz-probe2.mjs` on Contabo (temp, will clean up).
Playwright+Chromium installed under `/tmp/oztest` on Contabo (temp probe env, NOT the final install).

---
---

# Phase 1B — Moscow RU server (72.56.235.40) as the solution

**Date:** 2026-07-03 (same session, after user offered a Russian server).
**Server:** `My / seedon-site` — 72.56.235.40, Moscow, Timeweb (AS9123). `ssh root@72.56.235.40`.
Node v20.20.2, RAM total 2972 MB / **~2.3 GB available** (runs prod seedon.ru + CryptoBot + Xray-client).
⚠️ Second RU server SeedonRuInfra (89.23.102.244) — **DO NOT TOUCH** (user directive).

## Finding R1 — ✅ Moscow IP PASSES Ozon antibot (variant A viable)

**Confidence: CONFIRMED** (ran the repo's real code, got real data).

Installed Playwright 1.60 + Chromium on 72.56.235.40 and ran the **actual repo** `browser.js`:
```
[browser] passing anti-bot challenge…
[browser] challenge passed: OZON маркетплейс – миллионы товаров по в…
[repotest] ✓ SUCCESS in 12.9s
[repotest]   JSON length: 180118   has widgetStates: true
```
Real Ozon composer JSON (180 KB, `widgetStates`) in **12.9 s** on the first (antibot-paying) call.

### Contabo vs Moscow — the incident prefix tells the story
| | Contabo (FR) | Moscow (RU) |
|---|---|---|
| Incident id | `fab_nmk_…` | `fab_chlg_…` |
| Block page | "Выключите VPN…" (hard IP deny) | challenge offered (`challengeURL`) |
| Repo code result | 403, never passes | ✅ 200, passes in ~13s |

`fab_nmk` = hard geo/IP block (unsolvable). `fab_chlg` = a solvable JS challenge — the Moscow
IP has acceptable reputation; the antibot just wants the challenge solved, which a real
Chromium does.

### ⚠️ Critical lesson learned — DON'T over-stealth
My hand-rolled probes (both headless AND headful-under-xvfb) **failed** the `fab_chlg`
challenge because I added stealth patches: `navigator.webdriver` override, fake `plugins`,
fake `languages`, `timezoneId` override. **Variti detects clumsy stealth.** The repo's
**minimal, clean context** (just `userAgent` + `locale: ru-RU`, no init scripts) passes.
→ **Implementation rule: use the repo's config as-is; do NOT add stealth hacks.**

## Finding R2 — RAM budget on Moscow is TIGHT (must guard prod)

**Confidence: CONFIRMED** (measured) / peak still to be measured precisely in Phase 2.

- Moscow server: total **2972 MB**, available **~2344 MB**, ~628 MB used. This is the same
  3 GB box that previously **OOM-killed Kesha** — prod seedon.ru + CryptoBot live here.
- One Chromium ≈ 200–400 MB under load. Fits in 2.3 GB free, but **no comfortable margin** if
  something else spikes. Mitigations REQUIRED (see plan): repo's 10-min idle-close (frees to 0),
  `systemd MemoryMax`/cgroup cap on the Ozon process so it can NEVER starve seedon.ru/CryptoBot,
  `--single-process` is risky with Playwright (can break the challenge) — prefer the default
  multi-process + `--disable-dev-shm-usage` (already in repo args) + a hard MemoryMax.
- Peak per-request RAM to be measured under a MemoryMax before go-live.

## Finding R3 — SSH-stdio wiring: kesha@Contabo currently CANNOT reach Moscow

**Confidence: CONFIRMED** (tested).

- `sudo -u kesha ssh root@72.56.235.40` from Contabo → **Permission denied (publickey)**.
  kesha has keys (`id_ed25519`, `id_ed25519_cog`, both labelled `kesha@msk-1-vm-hdn1` — they
  were generated on the Moscow box during migration, used for GitHub) but **none are in the
  Moscow server's authorized_keys**.
- **Plan must add** kesha@Contabo's `id_ed25519.pub`
  (`ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBaxTkoSKRFXe/MFZWrnAwK9m/aYdtJ9YpkWApJBTi4T`)
  to a **dedicated restricted user** on Moscow (e.g. `ozon`), ideally with a forced command
  so the key can ONLY launch the MCP (`command="node /opt/ozon-mcp-server/src/index.js"`),
  not a full shell. Then Kesha's `.mcp.json` entry becomes:
  `"command": "ssh", "args": ["ozon@72.56.235.40", "..."]` — SSH transports stdio transparently.
- First Ozon call pays ~13s antibot + one SSH hop — acceptable for a chat bot.

## Finding R (region) — Krasnoyarsk is coordinate/addressbook-cookie driven; needs an impl spike

**Confidence: LIKELY on mechanism, UNCERTAIN on the cleanest automation** (measured a lot, not
yet a working flip).

What I proved:
- Default region from the Moscow IP = **"Москва"** (the label appears in composer JSON; verified).
- `/geo/krasnoyarsk/` is an **info page** (ПВЗ list) — navigating it does NOT change region.
- Naive tricks do NOT flip region: browser `geolocation` coords alone, `select_location=<GUID>`
  param alone, the `/geo/` page. Region stayed Москва in every case.
- The region selector is the **addressbook modal** (`/modal/addressbook`). Modern Ozon shows
  **"Выберите адрес доставки → Выбрать на карте"** — a **map picker only, no city text field**
  (screenshot captured) + a cookie-consent popup that must be dismissed first. Region is set by
  choosing an exact address / pickup point on an interactive map; the write goes through an
  `addressBook…`/coords POST and lands in cookies (`xcid`, `__Secure-ext_xcid`, address cookies).

Implication for the plan — **3 candidate strategies (rank in Phase 2, likely with Codex):**
1. **One-time map-picker capture → replay `storageState`.** Drive the map once (with
   `geolocation`=Krasnoyarsk so the map centers there, dismiss cookie consent, click a pickup
   pin, confirm), save `context.storageState()`, commit the Krasnoyarsk cookies, and load them
   into the repo's long-lived context. Region cookies look long-lived. *Most robust once it
   works; brittle to build (map automation).* Re-capture if cookies expire.
2. **Find the underlying coords-POST endpoint** the map "confirm" fires and call it directly
   with Krasnoyarsk coords after each fresh challenge (no map UI). *Cleanest if the endpoint is
   stable — needs to be sniffed precisely (I saw `addressBookBarTooltip` but that's a tooltip,
   not the setter).*
3. **`select_location=<Krasnoyarsk GUID>` on every composer URL** — a small patch to
   `src/ozon.js`. Reported to need an accompanying cookie/token, so likely only works combined
   with #1/#2. *Verify empirically.*

→ **Region needs a short implementation spike in Phase 3** (build flow #1, fall back to #2).
The blocker and the transport are SOLVED and CONFIRMED; region is an engineering detail with a
known solution space, not a blocker. Price-delta verification (same SKU Moscow vs Krasnoyarsk)
is an acceptance criterion for the region ticket.

## Updated bottom line

- ✅ **Ozon reachable** — from Moscow RU IP, via repo's real code (CONFIRMED).
- ✅ **Repo works** as-is; entry `src/index.js`; 10-min idle auto-close (CONFIRMED).
- ⚠️ **RAM tight** on the 3 GB Moscow prod box — mitigations mandatory (MemoryMax + idle-close).
- ⚠️ **SSH wiring** — add kesha's key as a restricted `ozon@72.56.235.40` user (forced command).
- ⚠️ **Region Krasnoyarsk** — solvable, needs an impl spike (strategy #1 storageState capture).

**Ready for Phase 2 (plan + Codex review).**

## Phase 1B sources
- [4] Live probes on 72.56.235.40, 2026-07-03: repo `browser.js` ✓ (raw logs above); antibot
      pass; region mechanism (addressbook map picker, screenshot `/tmp/oztest/modal1.png`).
- [5] Playwright anti-bot guidance 2026 — https://alterlab.io/blog/playwright-bot-detection-what-actually-works-in-2026 ; ZenRows/BrowserStack (over-stealth is detectable; clean minimal context + real browser + good IP wins).
