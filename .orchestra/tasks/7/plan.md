# Plan — Task #7: Ozon MCP server (Moscow RU host) + Krasnoyarsk region + wire to Kesha

**Based on:** `research.md` (Phase 1 + 1B). Blocker (Contabo FR IP-blocked) solved by hosting
the Ozon MCP on the user's Moscow server **72.56.235.40** (passes Ozon antibot, CONFIRMED live).

## Architecture decision

```
Kesha (Contabo FR, /opt/cog-second-brain)
  └─ .mcp.json  "ozon" server = ssh ozon@72.56.235.40 (stdio transported over SSH)
        │  (SSH-stdio; first call ~13s antibot, then 0.3–1s)
        ▼
Moscow server 72.56.235.40 (RU IP → Ozon lets it in, NO proxy needed)
  └─ /opt/ozon-mcp-server  (eduard256/ozon-mcp-server, unmodified antibot logic)
        └─ one long-lived headless Chromium → composer-api JSON
        └─ region forced to KRASNOYARSK via captured storageState cookies (abs path, fail-closed)
        └─ launched inside `systemd-run --scope -p MemoryMax=…` so Chromium+children
           are in ONE capped cgroup → can NEVER OOM prod seedon.ru / CryptoBot
```

Why SSH-stdio (not a proxy on Moscow): the Moscow server **is** the Russian exit. Running the
Chromium there needs no proxy at all. Kesha just needs to spawn the MCP process there over SSH;
MCP's stdio transport tunnels cleanly through SSH.

**⚠️ Codex-review fix (RAM cgroup):** a process spawned by `sshd` lands in a transient
`session-*.scope` (verified live on Moscow, systemd 255, `sshd Delegate=no`), NOT in any
`ozon-mcp.service` unit — so a unit-level `MemoryMax=` would be **decorative**. The wrapper
therefore launches the MCP via `systemd-run --scope -p MemoryMax=…` (see T3/T4) so Node **and**
all Chromium child processes sit in one enforced cgroup. `--scope` writes nothing to stdout
(status goes to stderr), so JSON-RPC stays clean.

**SSH contract (single, unambiguous):** the `ozon` key in `authorized_keys` carries a
`command="…wrapper…"` forced command; the `.mcp.json` side runs a **bare**
`ssh -T -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=2 ozon@72.56.235.40`
with **no remote command** (the forced command supplies it). This avoids the
`SSH_ORIGINAL_COMMAND` ambiguity Codex flagged.

## What changes where

- **Moscow 72.56.235.40** (NEW files only — do NOT touch seedon.ru/CryptoBot):
  - `/opt/ozon-mcp-server/` — clone repo, `npm ci`, `npx playwright install chromium`.
  - `src/browser.js` — **one surgical edit**: load Krasnoyarsk `storageState` into the context
    (the ONLY change to repo code; keep antibot logic untouched).
  - `/opt/ozon-mcp-server/krsk-state.json` — captured Krasnoyarsk region cookies. Delivered as an
    **ops file, `.gitignore`d by default** (may carry antibot/session identifiers — audit cookie
    names/domains/expiry, confirm NO auth/account cookies, before deciding to commit).
  - `/opt/ozon-mcp-server/ozon-mcp-wrapper.sh` — the forced-command wrapper:
    `exec systemd-run --scope --quiet -p MemoryMax=800M -p MemorySwapMax=0 \
      env HOME=/home/ozon node /opt/ozon-mcp-server/src/index.js`
    (`cd` into the dir, `exec`, all diagnostics to stderr — nothing to stdout).
  - New restricted user `ozon` (forced-command SSH key, no shell) — chosen in T3.
- **Contabo `/opt/cog-second-brain/.mcp.json`** — add ONE `"ozon"` stdio entry (SSH command).
  Do not touch the existing 5 servers.
- **This repo (docs/kesha-bot):** `CLAUDE.md` (+ozon MCP note), `CHANGELOG.md`, task docs.
- **NOT touched:** `rag.py` (colleague feat-rag-upgrade), Contabo bot Python code, seedon.ru.

## Tickets

### T1 — Install Ozon MCP on Moscow server + confirm it runs
- Files: Moscow `/opt/ozon-mcp-server/` (clone, `npm ci`, `npx playwright install chromium`).
  Move the temp `/tmp/oztest` + `/tmp/ozon-repo` probe env aside; do a clean install in /opt.
- Own the Chromium cache location (root vs ozon user HOME — must match the user that runs MCP).
- AC:
  - `node /opt/ozon-mcp-server/src/index.js` starts, logs `ready on stdio` to stderr.
  - A direct `fetchJson("/search/?text=iphone")` returns HTTP 200 JSON with `widgetStates`
    (re-run the proven repo test) — real Ozon data, from the Moscow IP.
  - `npm run test:parse` (offline parser tests) passes.
- blocked-by: none

### T2 — Force Krasnoyarsk region (impl spike: capture storageState → replay)
**⚠️ Highest-risk ticket. Krasnoyarsk prices are a HARD user requirement — NO silent
degradation to Moscow prices. If the map can't be automated even via a one-off MANUAL click,
STOP and escalate to orchestrator (do not burn hours guessing).**

- Files: `src/browser.js` (load `storageState`), `krsk-state.json` (captured cookies),
  `scripts/capture-region.mjs` (dev/ops tool — also the re-refresh tool, not in the hot path).
- Approach (research strategy #1, fallback #2):
  1. Script drives the addressbook map picker ONCE with `geolocation`=Krasnoyarsk
     (56.0106, 92.8526), dismisses cookie consent, selects a Krasnoyarsk pickup point,
     confirms; saves `context.storageState()` → `krsk-state.json`.
  2. `browser.js` `newContext({ storageState: <ABSOLUTE path>, ... })` — keep UA+locale,
     NO stealth patches (research lesson). **Codex fix:** resolve the path from the module, not
     cwd — `path.resolve(dirname(fileURLToPath(import.meta.url)), "../krsk-state.json")` — because
     the SSH forced-command may start in `/home/ozon`, and a relative string would silently miss
     the file → region falls back to Moscow.
  3. If map automation proves too brittle: sniff the coords-confirm POST and replay it after
     each challenge (strategy #2).
  4. **Fallback if map can't be clicked headless** (Variti/captcha on the modal): do the click
     ONCE in **headful** (xvfb on the server, or locally on a RU-reachable browser), export the
     cookies, upload `krsk-state.json` to the server. One-off manual op = acceptable. **Verify
     headless automatability FIRST; fall back to manual only if needed.**

- **Q1 — cookie TTL / refresh mechanic (MUST answer + cover):**
  - Measure/inspect the expiry of the Krasnoyarsk region cookies in `krsk-state.json`
    (`__Secure-ext_xcid`, `xcid`, address cookies — read their `expires`). Determine after how
    long / how many sessions Krasnoyarsk reverts to Moscow.
  - Ship a **FAIL-CLOSED region self-check** (Codex fix — LOUD log alone is NOT enough for a
    mandatory requirement): before serving the first tool call after each launch/relaunch and
    after any 403/307 re-challenge, read the region marker from a composer `/` response. If it's
    NOT Krasnoyarsk → try one state reload; if STILL not Krasnoyarsk → the tool returns an MCP
    **`isError`** (not Moscow data). The bot must never hand the user Moscow prices silently.
    Не «настроил и забыл».
  - Provide the **refresh path**: re-run `scripts/capture-region.mjs` (manual or cron) to
    regenerate `krsk-state.json`. Document cadence once TTL is known (e.g. weekly cron, or
    on-detect re-capture). Pick the simplest that holds Krasnoyarsk reliably.

- AC:
  - After load, a composer `/` response's region marker == **Красноярск** (not Москва).
  - **Price delta proof:** the SAME in-stock SKU returns a different price/delivery for
    Krasnoyarsk vs Moscow (measure both, record numbers in report). If prices legitimately
    equal for a SKU, verify via a region-labelled field instead (delivery city name).
  - Antibot still passes with the state loaded (challenge not broken by injected cookies).
  - **Cookie TTL documented** (measured expiry) + a **detect-and-refresh** mechanic exists
    (region self-check that fires when Krasnoyarsk reverts; documented refresh command/cron).
  - `krsk-state.json` in `.gitignore`; report lists its cookie names/domains/expiry and confirms
    NO auth/account cookies (Codex suggestion).
- blocked-by: T1

### T3 — SSH-stdio wiring: restricted `ozon` user + pristine-stdout wrapper
- Files: Moscow `~ozon/.ssh/authorized_keys`, `/opt/ozon-mcp-server/ozon-mcp-wrapper.sh`.
- **Single SSH contract** (Codex fix — no ambiguity): dedicated `ozon` user (no shell login);
  its `authorized_keys` line = `command="/opt/ozon-mcp-server/ozon-mcp-wrapper.sh",no-pty,no-port-forwarding,no-X11-forwarding,no-agent-forwarding <kesha pubkey>`.
  The `.mcp.json` side runs a **bare** ssh with **no remote command**:
  `ssh -T -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=2 ozon@72.56.235.40`.
  The forced command supplies the launch; `SSH_ORIGINAL_COMMAND` is ignored.
- **Pristine stdout** (Codex fix): wrapper is exactly
  `#!/bin/bash` + `cd /opt/ozon-mcp-server` + `exec systemd-run --scope --quiet -p MemoryMax=800M -p MemorySwapMax=0 env HOME=/home/ozon node src/index.js`.
  No `echo`/banner/MOTD to stdout; `PrintMotd no` for this session; `systemd-run --quiet` keeps
  its status off stdout. Repo already logs only to stderr.
- AC:
  - From Contabo: `sudo -u kesha ssh -T … ozon@72.56.235.40` launches ONLY the MCP; no shell,
    no forwarding.
  - **First byte of stdout on `initialize` is a JSON-RPC response** (no prefix/banner) — raw
    handshake returns the 3 ozon tools.
  - EOF/orphan test: kill the local `ssh` → within seconds NO `node`/`chromium` left on Moscow
    (repo `transport.onclose`→`cleanup` fires); a fresh spawn re-`initialize`s cleanly.
- blocked-by: T1

### T4 — RAM safety: enforced cgroup cap (systemd-run --scope) + idle-close verified
- Files: the `MemoryMax` value in `ozon-mcp-wrapper.sh` (T3); no unit file needed.
- **Codex fix — the cap MUST bind the real SSH-spawned process tree, not a decorative unit.**
  Because sshd puts the process in `session-*.scope` (verified live: systemd 255, `Delegate=no`),
  we launch via `systemd-run --scope -p MemoryMax=… -p MemorySwapMax=0` from the wrapper — this
  creates a transient scope cgroup that contains Node **and** all Chromium children (cgroup v2
  is hierarchical). MemoryMax on a scope covers descendants.
- AC:
  - **Cgroup proof:** after a real request, `cat /proc/<node-pid>/cgroup` and every
    `chromium`/`chrome` child share ONE `…run-*.scope` (show via `systemd-cgls` /
    `ps --forest`), and `memory.max` == the cap (Codex AC).
  - Measured **peak `MemoryCurrent`** of a heavy Ozon request recorded; cap set above peak with
    margin yet ≤ ~700–800 MB so seedon.ru/CryptoBot can never be starved.
  - Kill-test: force a request to exceed the cap → OOM-killer hits ONLY the ozon scope; verify
    seedon.ru + CryptoBot still up and `journalctl -k` shows no oom-kill of prod.
  - After 10 min idle, Chromium gone (scope RSS→0); relaunches on next call.
- blocked-by: T1

### T5 — Add `ozon` to Kesha `.mcp.json` (Contabo) without breaking the 5 existing servers
- Files: Contabo `/opt/cog-second-brain/.mcp.json`. Add ONE stdio entry — **bare ssh, no remote
  command** (the forced command supplies the launch):
  `"ozon": {"type":"stdio","command":"ssh","args":["-T","-o","BatchMode=yes","-o","ServerAliveInterval=30","-o","ServerAliveCountMax=2","ozon@72.56.235.40"]}`
  (run as user `kesha`, whose key is authorized on Moscow).
- AC:
  - `.mcp.json` is valid JSON (`jq .` passes); the 5 existing servers (kesha, yougile, mailru,
    gmail, websearch) are byte-identical (diff shows +ozon only).
  - Backup of the original `.mcp.json` taken before edit.
- blocked-by: T3

### T6 — End-to-end verify from Kesha + docs
- Files: `CLAUDE.md` (MCP list +ozon), `CHANGELOG.md`, `docs/tasks/7/report.md`.
- Steps: coordinate restart of `kesha-bot-vps` with orchestrator (colleague feat-rag-upgrade also
  restarts the bot — **must sync via orchestrator**). After restart, confirm `mcp__ozon__*` tools
  load and a real query returns **Krasnoyarsk** prices.
- AC:
  - `ozon_search` / `ozon_product_details` / `ozon_product_reviews` callable from Kesha, return data.
  - A search returns Krasnoyarsk-region prices (spot-check one product).
  - **Cold-call timeout check (Codex):** measure the FIRST call after an idle-close (pays the
    ~13s antibot + SSH hop) end-to-end from Kesha — confirm the bot's MCP client doesn't time it
    out (repo tool timeout is 55s; verify the claude_agent_sdk side tolerates ~15-20s cold call).
  - RAM on Moscow post-run within budget; seedon.ru + CryptoBot still healthy.
  - Docs updated; CHANGELOG entry added.
- blocked-by: T2, T4, T5

## Ordering
T1 → (T2, T3, T4 in parallel) → T5 → T6.

## Risks / mitigations
- **Region automation brittle** (map picker) → spike in T2 with fallbacks: (a) coords-POST
  replay, (b) one-off MANUAL headful click → export cookies. **Krasnoyarsk is mandatory — do
  NOT silently degrade to Moscow prices.** If NONE of {headless click, coords-POST, manual
  weekly re-click} work → **STOP and escalate to orchestrator/user**, don't hack around it.
- **Region cookie expiry** → measure TTL (T2 Q1); **fail-closed** region self-check (revert →
  reload → still wrong → tool `isError`, never Moscow data) + documented refresh (T2).
- **OOM on prod Moscow box** → cap bound to the real process tree via `systemd-run --scope`
  (NOT a decorative unit MemoryMax — Codex fix), cgroup-verified + idle-close + kill-test BEFORE
  wiring to Kesha (T4).
- **SSH forced-command / stdout** → single contract (bare ssh + forced wrapper), pristine stdout,
  EOF/orphan-Chromium smoke (T3).
- **Cold-call timeout** → verify bot MCP client tolerates the ~13s first-call antibot (T6).
- **Bot-restart collision** with feat-rag-upgrade → orchestrator sequences restarts (T6).
- **SSH key too broad** → forced command, dedicated `ozon` user, no shell, no forwarding (T3).

## What NOT to do
- Don't add stealth patches to `browser.js` (breaks antibot — research-proven).
- Don't touch seedon.ru / CryptoBot / SeedonRuInfra (89.23.102.244).
- Don't touch `rag.py` or other colleague files.
- Don't restart `kesha-bot-vps` without orchestrator sign-off (colleague coordination).
- Don't commit personal/account cookies — region state only, audited first.
