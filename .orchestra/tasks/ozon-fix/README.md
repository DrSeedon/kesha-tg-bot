# Ozon MCP prod fixes — Moscow server (72.56.235.40, user `ozon`, `/opt/ozon-mcp-server`)

Two systemic bugs fixed + deployed. NOT the kesha-tg-bot repo — the Ozon MCP is a separate
repo on the Moscow (RU-IP) server. Patches copied here for record; deployed live via scp/systemd.

## Bug #1 — zombie node processes (RAM leak → OOM risk for seedon.ru)

**Cause:** MCP SSH session is long-lived (`ssh -T` per `.mcp.json`). When SSH stales
(network/timeout), sshd+node linger, EOF-cleanup never fires → orphaned Chromium accumulates.

**Fix:** `kill-stale.sh` (root, systemd timer every 20 min). Kills ozon `index.js` nodes whose
**parent sshd has no ESTABLISHED socket** (or PPID=1) AND age > 2h. Socket-state, not pure age —
a healthy long session is hours old, so age-only killing would nuke live sessions.

**⚠️ MUST run as root.** `ss` only reveals socket→pid for the caller's own sockets; the MCP's
parent sshd socket is root-owned. As user `ozon`, `ss` returns 0 pids → EVERY node looks orphaned
→ kills LIVE sessions. **Verified empirically** (ran as ozon during dev → killed the active node).
Hence root-only systemd timer, NOT the ozon wrapper.

**Deployed:** `/opt/ozon-mcp-server/kill-stale.sh` (root:root 755),
`ozon-kill-stale.{service,timer}` (OnUnitActiveSec=20min). First run clean (0 kills).

## Bug #2 — region cookies slip (krsk-state.json → Moscow prices)

**Cause:** Krasnoyarsk region cookies observed to expire ~6 days (not the 365d TTL). browser.js
has a fail-closed self-check (throws if region != Krasnoyarsk) but no recovery → tool just errors.

**Fix (two layers):**
1. **Reactive** — `browser.js` patched: `ensureContext` now, on region-check failure, runs
   `refresh-region.sh` once (closes its own browser first → no 2 Chromiums under the 800M cap),
   then retries with fresh cookies. `regionRefreshTried` guard prevents loops; reset on healthy
   region. Still fail-closed if refresh doesn't fix it.
2. **Preventive** — `refresh-region.sh` + `ozon-region-refresh.timer` every 5 days (04:30).

**refresh-region.sh:** `flock` single-flight → `capture-region.mjs headless` (writes
`/tmp/krsk-state.json` ONLY if it lands in Krasnoyarsk, fail-closed) → validate JSON → atomic
`mv` into live `krsk-state.json`. Old state kept if capture fails.

**Deployed + verified:** patched browser.js (backup `browser.js.bak-*`), MCP end-to-end OK
(region=Красноярск, fetchJson 81KB), refresh-region.sh tested (captured + installed fresh state).

## Files
- `kill-stale.sh` → `/opt/ozon-mcp-server/kill-stale.sh` (root)
- `refresh-region.sh` → `/opt/ozon-mcp-server/refresh-region.sh` (ozon)
- `browser.js.patched` → `/opt/ozon-mcp-server/src/browser.js` (ozon)
- systemd: `ozon-kill-stale.{service,timer}`, `ozon-region-refresh.{service,timer}` (in /etc/systemd/system)

## Not tested (accepted)
- Real end-to-end zombie kill (couldn't simulate a staled SSH from the server — root key not in
  ozon authorized_keys). Detection logic verified live: active sshd-parented node correctly seen
  as LIVE by root `ss`. The 20-min timer + 2h age guard makes false-positives impossible.
- spawnSync blocks the event loop up to 180s during reactive refresh — acceptable (MCP already
  broken when region stale; rare, ~every 6 days; 2-user scale).
