# Migration Plan — Kesha Bot: Timeweb → Contabo (#6)

**Source:** `root@72.56.235.40` (Timeweb, Moscow, kesha=uid 1001) — root SSH works, deploy has NOPASSWD ALL.
**Target:** `root@158.220.127.161` (Contabo, France, 8GB, Ubuntu 24.04) — root SSH works, /opt empty.
**Relay:** this laptop (`/mnt/data` 772G free) — both servers accept laptop's SSH key. rsync has no remote↔remote, so laptop stages a tarball.
**Downtime:** small window acceptable. Bot stopped only for final DB sync + cutover.

**Guiding invariants:**
- Keep CWD path **identical** (`/opt/cog-second-brain`) → `~/.claude/projects/-opt-cog-second-brain/` dir name matches → sessions resume with full memory.
- Keep kesha **uid:gid = 1001:1001** → ownership transfers cleanly.
- **Never delete Timeweb data** until Contabo verified. Timeweb bot = instant rollback.

---

## Phase A — Prepare Contabo (no impact on prod)

### A1. Create users (matching uid) — with preflight (Codex: uid 1001 must be free)
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
set -e
# PREFLIGHT: abort if uid/gid 1001 already taken by a DIFFERENT user (else ownership breaks)
if getent passwd 1001 | grep -qv '^kesha:'; then echo "FATAL: uid 1001 taken: $(getent passwd 1001)"; exit 1; fi
if getent group  1001 | grep -qv '^kesha:'; then echo "FATAL: gid 1001 taken: $(getent group 1001)"; exit 1; fi
# kesha with SAME uid:gid as Timeweb (1001) for clean ownership transfer
getent group kesha  >/dev/null || groupadd -g 1001 kesha
getent passwd kesha >/dev/null || useradd -u 1001 -g 1001 -m -s /bin/bash kesha
# tunnel user: restricted, nologin (reverse SSH tunnel target)
getent passwd tunnel >/dev/null || useradd -r -s /usr/sbin/nologin -m -d /home/tunnel tunnel
id kesha; id tunnel
EOF
```

### A2. Install runtime (node 20, claude CLI, build deps)
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
set -e
apt-get update
apt-get install -y python3-venv python3-pip rsync curl git
# Node 20 (nodesource) — for websearch MCP + claude CLI
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @anthropic-ai/claude-code
node --version; npm --version; which claude
EOF
```
*Note: claude CLI is npm-global on old host (`@anthropic-ai/claude-code`). Same here.*

### A3. Create target dirs owned by kesha
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
set -e
mkdir -p /opt/kesha-bot /opt/cog-second-brain
chown kesha:kesha /opt/kesha-bot /opt/cog-second-brain
EOF
```

---

## Phase B — Stage data from Timeweb (bot STILL RUNNING except final DB step)

All staging on laptop under `/mnt/data/kesha-migration/`. We tar as root on Timeweb (reads 0600 files), pull to laptop, push to Contabo.

### B0. Capture the uncommitted claude_session.py tweak FIRST (safety)
```bash
mkdir -p /mnt/data/kesha-migration
ssh root@72.56.235.40 'git -C /opt/kesha-bot diff claude_session.py' \
  > /mnt/data/kesha-migration/claude_session.tweak.diff
cat /mnt/data/kesha-migration/claude_session.tweak.diff   # must show thinking/effort
```

### B1. Capture MCP venv dependency lists (source of truth for recreation)
```bash
ssh root@72.56.235.40 bash -s <<'EOF' > /dev/null
for m in yougile-mcp mailru-mcp gmail-mcp; do
  v=/home/kesha/.claude/mcp-servers/$m/venv/bin/pip
  [ -x "$v" ] && $v freeze > /home/kesha/.claude/mcp-servers/$m/FREEZE.txt
done
EOF
# (FREEZE.txt travels inside the mcp-servers tarball in B4)
```

### B2. Stage code + config (bot still running — these files are static)
```bash
# kesha-bot code: EXCLUDE venv/pycache/logs, INCLUDE .env, system_prompt.txt, storage handled separately
ssh root@72.56.235.40 'tar czf - -C /opt \
  --exclude=kesha-bot/.venv \
  --exclude=kesha-bot/__pycache__ \
  --exclude=kesha-bot/logs \
  --exclude=kesha-bot/storage \
  kesha-bot' > /mnt/data/kesha-migration/kesha-bot-code.tgz

# cog-second-brain (CWD/vault) — exclude .git optional? keep it for future git pull
ssh root@72.56.235.40 'tar czf - -C /opt cog-second-brain' \
  > /mnt/data/kesha-migration/cog.tgz

# Claude CLI state: projects (session histories 242M) + auth + config + mcp-servers CODE
# mcp-servers: exclude venv + node_modules (recreate), keep code + FREEZE.txt + gmail creds
ssh root@72.56.235.40 'tar czf - -C /home/kesha \
  --exclude=.claude/debug \
  --exclude=.claude/telemetry \
  --exclude=.claude/cache \
  --exclude=.claude/backups \
  --exclude=.claude/mcp-servers/yougile-mcp/venv \
  --exclude=.claude/mcp-servers/mailru-mcp/venv \
  --exclude=.claude/mcp-servers/gmail-mcp/venv \
  --exclude=.claude/mcp-servers/websearch/node_modules \
  .claude .claude.json .ssh .google_workspace_mcp' \
  > /mnt/data/kesha-migration/kesha-home.tgz

ls -lh /mnt/data/kesha-migration/
```

### B3. Verify tarball integrity, push to Contabo, verify checksum, extract as kesha
```bash
# Codex: guard against partial/truncated tarballs before trusting them
cd /mnt/data/kesha-migration
for f in kesha-bot-code.tgz cog.tgz kesha-home.tgz; do
  tar tzf "$f" >/dev/null || { echo "CORRUPT: $f"; exit 1; }
  sha256sum "$f"
done | tee checksums.txt
ls -lh *.tgz

scp kesha-bot-code.tgz cog.tgz kesha-home.tgz checksums.txt root@158.220.127.161:/tmp/

# verify checksums survived transfer, and tarballs valid, BEFORE extract
ssh root@158.220.127.161 'cd /tmp && sha256sum -c checksums.txt && for f in kesha-bot-code.tgz cog.tgz kesha-home.tgz; do tar tzf $f >/dev/null || { echo "CORRUPT ON CONTABO: $f"; exit 1; }; done && echo "tarballs OK"'

ssh root@158.220.127.161 bash -s <<'EOF'
set -e
tar xzf /tmp/kesha-bot-code.tgz -C /opt         # → /opt/kesha-bot (code)
tar xzf /tmp/cog.tgz            -C /opt          # → /opt/cog-second-brain
sudo -u kesha tar xzf /tmp/kesha-home.tgz -C /home/kesha   # → .claude, .ssh, etc as kesha
chown -R kesha:kesha /opt/kesha-bot /opt/cog-second-brain /home/kesha/.claude /home/kesha/.claude.json /home/kesha/.ssh /home/kesha/.google_workspace_mcp
chmod 700 /home/kesha/.ssh; chmod 600 /home/kesha/.ssh/* 2>/dev/null || true
chmod 600 /home/kesha/.claude/.credentials.json
ls -la /opt/kesha-bot | head; echo '---'; du -sh /home/kesha/.claude/projects
EOF
```

### B4. Apply the uncommitted tweak on Contabo
```bash
scp /mnt/data/kesha-migration/claude_session.tweak.diff root@158.220.127.161:/tmp/
ssh root@158.220.127.161 'cd /opt/kesha-bot && sudo -u kesha git apply /tmp/claude_session.tweak.diff && sudo -u kesha git diff claude_session.py | head'
# Verify thinking={"type":"adaptive"} + effort="high" present
```

---

## Phase C — Recreate environments on Contabo

### C1. Bot .venv from requirements.txt
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
set -e
cd /opt/kesha-bot
sudo -u kesha python3 -m venv .venv
sudo -u kesha .venv/bin/pip install --upgrade pip
sudo -u kesha .venv/bin/pip install -r requirements.txt
sudo -u kesha .venv/bin/python -c "import aiogram, claude_agent_sdk, sqlite_vec, fastembed; print('deps ok')"
EOF
```

### C2. MCP server venvs + node_modules
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
set -e
BASE=/home/kesha/.claude/mcp-servers
# yougile: has requirements.txt
cd $BASE/yougile-mcp && sudo -u kesha python3 -m venv venv && sudo -u kesha venv/bin/pip install -r requirements.txt
# mailru: no requirements → use FREEZE.txt captured in B1
cd $BASE/mailru-mcp && sudo -u kesha python3 -m venv venv && sudo -u kesha venv/bin/pip install -r FREEZE.txt
# gmail: pip package workspace-mcp==1.20.3 (FREEZE.txt authoritative)
cd $BASE/gmail-mcp && sudo -u kesha python3 -m venv venv && sudo -u kesha venv/bin/pip install -r FREEZE.txt
# websearch: node
cd $BASE/websearch && sudo -u kesha npm install
EOF
```
*If mailru/gmail FREEZE.txt has editable/local paths, fall back: yougile `requirements.txt`, gmail `pip install workspace-mcp==1.20.3`, mailru inspect `server.py` imports (likely just `mcp`/`fastmcp`+`requests`).*

---

## Phase D — Edit config (drop proxy)

### D1. Rewrite .env (remove ALL proxy vars, kill failover flag)
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
set -e
cd /opt/kesha-bot
# strip proxy + KESHA_PRIORITY lines
sudo -u kesha sed -i \
  -e '/PROXY/Id' -e '/proxy/d' -e '/^no_proxy/Id' -e '/^NO_PROXY/Id' -e '/KESHA_PRIORITY/d' \
  .env
echo 'KESHA_PRIORITY=primary' | sudo -u kesha tee -a .env >/dev/null
sudo -u kesha cat .env
EOF
```
*Verify: no HTTPS_PROXY/HTTP_PROXY/TG_PROXY/NO_PROXY remain; TELEGRAM_BOT_TOKEN, DEEPGRAM_API_KEY, WORK_DIR=/opt/cog-second-brain intact.*

### D1.5 Hunt residual proxy references in ALL configs (Codex: proxy may lurk elsewhere)
```bash
ssh root@158.220.127.161 'grep -rniE "HTTPS?_PROXY|TG_PROXY|NO_PROXY|127\.0\.0\.1:10809|:10809" \
  /opt/kesha-bot/.env /opt/cog-second-brain/.mcp.json \
  /home/kesha/.claude.json /home/kesha/.claude/settings.json \
  /home/kesha/.claude/mcp-servers 2>/dev/null || echo "CLEAN — no residual proxy refs"'
```
*Only .env legitimately had proxy (removed in D1). If anything else shows a real proxy setting → remove manually. `.mcp.json` env blocks are per-server API keys, not proxy — leave those.*

### D2. systemd unit (no proxy)
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
cat > /etc/systemd/system/kesha-bot-vps.service <<'UNIT'
[Unit]
Description=Kesha Telegram Bot (Contabo)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kesha
WorkingDirectory=/opt/kesha-bot
EnvironmentFile=/opt/kesha-bot/.env
Environment=HOME=/home/kesha
ExecStart=/opt/kesha-bot/.venv/bin/python3 bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
EOF
```
*Keep service name `kesha-bot-vps` → CLAUDE.md deploy commands stay valid.*

### D3. tunnel user authorized_keys (reverse tunnel target)
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
set -e
mkdir -p /home/tunnel/.ssh
cat > /home/tunnel/.ssh/authorized_keys <<'KEY'
restrict,port-forwarding,permitlisten="127.0.0.1:2222" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC6j7jgnzbGPgcBN3GF5BwpG/F5Tx0xPfD7g1SIduPVJ reverse-tunnel-to-vps
KEY
chown -R tunnel:tunnel /home/tunnel/.ssh
chmod 700 /home/tunnel/.ssh; chmod 600 /home/tunnel/.ssh/authorized_keys
# sshd must allow AllowTcpForwarding for -R to 127.0.0.1 (default clientspecified ok)
grep -qE '^AllowTcpForwarding' /etc/ssh/sshd_config || echo 'AllowTcpForwarding yes' >> /etc/ssh/sshd_config
systemctl reload ssh || systemctl reload sshd
# Codex: verify EFFECTIVE config for tunnel user (Match blocks could still block forwarding)
sshd -T -C user=tunnel,host=localhost,addr=127.0.0.1 2>/dev/null | grep -iE 'allowtcpforwarding|permitlisten|gatewayports' || echo "(sshd -T check)"
EOF

# Real end-to-end test from laptop: can we open the reverse forward as tunnel@Contabo?
ssh -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes -o ConnectTimeout=10 \
  -i ~/.ssh/tunnel_vps -N -R 127.0.0.1:2222:localhost:22 tunnel@158.220.127.161 &
_T=$!; sleep 4
ssh root@158.220.127.161 "ss -ltn | grep ':2222' && echo 'reverse-forward OK'" || echo 'FORWARD FAILED'
kill $_T 2>/dev/null
```

---

## Phase E — Claude auth verification (on Contabo, as kesha)

```bash
ssh root@158.220.127.161 'sudo -u kesha -H bash -lc "cd /opt/cog-second-brain && claude --version && echo === && timeout 30 claude -p \"say READY\" 2>&1 | head"'
```
- If it answers → copied `.credentials.json` works (refreshToken renewed access). ✅
- If auth error → fallback: `ssh root@158.220.127.161` → `sudo -u kesha -i` → `claude auth login` (Contabo direct, NO proxy) → browser flow → paste code.

---

## Phase F — CUTOVER (the only real downtime)

> Phases A–E ran with old bot LIVE. B2 already staged an *early* copy of `storage/` and
> `~/.claude/projects/`, but the user kept chatting since → those are STALE. Phase F re-syncs
> **both** the DBs AND the session `.jsonl` histories after stopping the bot, so nothing added
> between B2 and cutover is lost. (Codex blocker #1.)

### F1. Stop old bot + HARD process gate (Codex blocker #3: guarantee no poller left)
```bash
ssh root@72.56.235.40 bash -s <<'EOF'
set -e
systemctl stop kesha-bot-vps || true
sleep 2
# is-active returns "inactive"(rc=3) when stopped — that's SUCCESS here, so check the WORD
state=$(systemctl is-active kesha-bot-vps || true)
echo "service state: $state"
[ "$state" = "active" ] && { echo "FATAL: service still active"; exit 1; }
# no stray bot.py process may keep polling the TG token
if pgrep -af 'bot\.py' | grep -v pgrep; then echo "FATAL: stray bot.py poller running"; exit 1; fi
# also ensure no OTHER enabled kesha unit re-spawns it
systemctl list-units --type=service | grep -i kesha || true
echo "TIMEWEB QUIESCED — no poller"
EOF
```
*Only ONE process may poll the TG token. This gate + F4 (Contabo start) being strictly after → never two pollers → no `Conflict: terminated by other getUpdates`.*

### F2. WAL checkpoint all 3 DBs on Timeweb + integrity check (Codex: validate result)
```bash
ssh root@72.56.235.40 bash -s <<'EOF'
set -e
for db in messages reminders vec; do
  python3 - "$db" <<PY
import sqlite3,sys
p=f"/opt/kesha-bot/storage/{sys.argv[1]}.db"
c=sqlite3.connect(p)
busy,logf,ckpt = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
print(sys.argv[1],"checkpoint busy=%s log=%s ckpt=%s"%(busy,logf,ckpt))
if busy != 0:
    print("FATAL: checkpoint busy!=0 (writer still active)"); sys.exit(1)
qc = c.execute("PRAGMA quick_check").fetchone()[0]
print(sys.argv[1],"quick_check:",qc)
if qc != "ok":
    print("FATAL: integrity check failed"); sys.exit(1)
c.close()
PY
done
ls -la /opt/kesha-bot/storage/*.db*   # -wal should be ~0 bytes now
EOF
```
*Checkpoint(TRUNCATE) with busy==0 flushes ALL WAL into main .db and truncates -wal. quick_check=ok confirms no corruption. We still copy -wal/-shm too (belt-and-suspenders).*

### F3. Final sync of BOTH storage AND session histories (Codex blocker #1)
```bash
# (a) storage: DBs + sessions/ + media/
ssh root@72.56.235.40 'tar czf - -C /opt/kesha-bot storage' > /mnt/data/kesha-migration/storage.tgz
# (b) FRESH session histories — the .jsonl that grew since B2
ssh root@72.56.235.40 'tar czf - -C /home/kesha .claude/projects/-opt-cog-second-brain' \
  > /mnt/data/kesha-migration/projects-final.tgz

# integrity before trusting
for f in storage projects-final; do tar tzf /mnt/data/kesha-migration/$f.tgz >/dev/null || { echo "CORRUPT $f"; exit 1; }; done
cd /mnt/data/kesha-migration && sha256sum storage.tgz projects-final.tgz > checksums-final.txt

scp storage.tgz projects-final.tgz checksums-final.txt root@158.220.127.161:/tmp/
ssh root@158.220.127.161 'cd /tmp && sha256sum -c checksums-final.txt'

ssh root@158.220.127.161 bash -s <<'EOF'
set -e
# overwrite stale early copies with the final ones
sudo -u kesha tar xzf /tmp/storage.tgz         -C /opt/kesha-bot
sudo -u kesha tar xzf /tmp/projects-final.tgz  -C /home/kesha   # → .claude/projects/-opt-cog-second-brain
chown -R kesha:kesha /opt/kesha-bot/storage /home/kesha/.claude/projects

# verify DB row counts
for db in messages reminders vec; do
 sudo -u kesha python3 - "$db" <<PY
import sqlite3,sys
c=sqlite3.connect(f"/opt/kesha-bot/storage/{sys.argv[1]}.db")
t=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
for n in t:
  try: print(f"{sys.argv[1]}.{n}", c.execute(f"SELECT count(*) FROM {n}").fetchone()[0])
  except Exception as e: print(f"{sys.argv[1]}.{n} ? {e}")
PY
done

# CRITICAL: every active session UUID must have its .jsonl on Contabo (else empty memory)
for cid in 720740564 893553748; do
  uuid=$(cat /opt/kesha-bot/storage/sessions/$cid)
  jf="/home/kesha/.claude/projects/-opt-cog-second-brain/$uuid.jsonl"
  if [ -s "$jf" ]; then echo "session $cid → $uuid : $(du -h $jf | cut -f1) OK";
  else echo "FATAL: session $cid uuid=$uuid has NO .jsonl history"; exit 1; fi
done
EOF
```

### F4. Start bot on Contabo
```bash
ssh root@158.220.127.161 bash -s <<'EOF'
set -e
# smoke import first
sudo -u kesha -H bash -lc 'cd /opt/kesha-bot && HOME=/home/kesha .venv/bin/python -c "import bot; print(\"import ok\")"'
systemctl enable kesha-bot-vps
systemctl start kesha-bot-vps
sleep 5
systemctl status kesha-bot-vps --no-pager | head -12
journalctl -u kesha-bot-vps --no-pager -n 40
EOF
```

### F5. Re-point laptop reverse tunnel to Contabo (Codex: host key BEFORE restart)
```bash
# 1. accept Contabo host key FIRST so the unit's ssh won't fail on unknown host
ssh-keyscan -H 158.220.127.161 >> ~/.ssh/known_hosts 2>/dev/null
# 2. flip IP in the unit
sudo sed -i 's/72\.56\.235\.40/158.220.127.161/' /etc/systemd/system/ssh-tunnel-vps.service
sudo systemctl daemon-reload
sudo systemctl restart ssh-tunnel-vps
sleep 4
sudo systemctl status ssh-tunnel-vps --no-pager | head -6
# 3. confirm the reverse forward landed on Contabo
ssh root@158.220.127.161 "ss -ltn | grep ':2222' && echo 'tunnel UP'" || echo 'TUNNEL DOWN — check unit'
```

---

## Phase G — VERIFY Contabo (before touching Timeweb)

Live smoke tests (via Telegram, done by user + log inspection):
1. **Bot answers** — send a message in TG, bot replies (streaming works).
2. **Sessions intact** — bot remembers prior context (ask about something from history).
3. **RAG search** — trigger `search_memory` (ask "что мы обсуждали про X"), returns real hits from vec.db.
4. **Reminders** — `list_reminders` shows existing; create+cancel a test reminder.
5. **MCP servers load** — check journalctl for MCP init, test yougile/gmail/websearch tool.
6. **run_on_laptop** — bot runs a whitelisted cmd on laptop (proves reverse tunnel).
7. **Voice** — send a voice msg → Deepgram transcribes (proves DEEPGRAM key + direct net).

Log check:
```bash
ssh root@158.220.127.161 'journalctl -u kesha-bot-vps --no-pager -n 80 | grep -iE "error|traceback|proxy|mcp|rag|OOM" | head'
ssh root@158.220.127.161 'free -h'   # RAM sanity (should be comfortable on 8GB)
```

**GATE: only after user confirms all 7 pass → Phase H.**

---

## Phase H — Decommission Timeweb (NO data deletion)

```bash
ssh root@72.56.235.40 'systemctl stop kesha-bot-vps && systemctl disable kesha-bot-vps && systemctl is-enabled kesha-bot-vps 2>&1'
# Data left INTACT on Timeweb (rollback safety). Do NOT rm anything.
```

Update docs (separate commit in worktree):
- `CLAUDE.md` (project): new IP 158.220.127.161, `ssh root@` (or create deploy user), remove proxy from troubleshooting, note no-proxy.
- `~/.claude/docs/vps-registry.md`: mark Timeweb kesha as decommissioned, add Contabo kesha entry.

---

## ROLLBACK (if Contabo fails at any point after F1)

Timeweb's **business data is intact** (F2 checkpoint physically rewrites .db/-wal but preserves all logical rows; we never delete or logically alter data). So Timeweb can resume as authoritative source.

**Step 0 — snapshot Contabo delta FIRST (Codex blocker #2: don't silently lose Contabo writes).**
If Contabo already ran the bot (any smoke/live turns), it may hold newer messages/RAG/`.jsonl` than Timeweb. Capture before rollback so the delta isn't lost:
```bash
ssh root@158.220.127.161 'systemctl stop kesha-bot-vps 2>/dev/null || true'   # stop writers first
ssh root@158.220.127.161 bash -s <<'EOF'
for db in messages reminders vec; do
 sudo -u kesha python3 -c "import sqlite3;sqlite3.connect('/opt/kesha-bot/storage/$db.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')" 2>/dev/null || true
done
sudo -u kesha tar czf /tmp/contabo-delta-storage.tgz  -C /opt/kesha-bot storage
sudo -u kesha tar czf /tmp/contabo-delta-projects.tgz -C /home/kesha .claude/projects/-opt-cog-second-brain
EOF
scp root@158.220.127.161:/tmp/contabo-delta-*.tgz /mnt/data/kesha-migration/
```
Then DECIDE (user call):
- **Verification-only smoke ran** (disposable test msgs) → discard delta, proceed.
- **Real conversation happened on Contabo** → merge delta back to Timeweb before restart (session `.jsonl` are append-only per-UUID → the newer file wins; DBs → compare row counts, keep the larger/newer). Only then start Timeweb.

**Step 1-3 — restore Timeweb as active:**
```bash
# 1. ensure Contabo bot fully quiesced — FATAL gate (mirror F1, Codex R2): no poller may remain
ssh root@158.220.127.161 bash -s <<'EOF'
systemctl stop kesha-bot-vps 2>/dev/null || true
systemctl disable kesha-bot-vps 2>/dev/null || true
sleep 2
state=$(systemctl is-active kesha-bot-vps || true)
[ "$state" = "active" ] && { echo "FATAL: contabo service still active"; exit 1; }
if pgrep -af 'bot\.py' | grep -v pgrep; then echo "FATAL: stray bot.py on Contabo — kill before rollback"; exit 1; fi
echo "CONTABO QUIESCED — safe to start Timeweb"
EOF
# ↑ ssh propagates the remote exit code. If it printed FATAL / returned non-zero → STOP, do NOT run step 2.
# 2. start Timeweb bot ONLY if step 1 exited 0; confirm by WORD not rc
ssh root@72.56.235.40 'systemctl start kesha-bot-vps && sleep 3 && [ "$(systemctl is-active kesha-bot-vps)" = active ] && echo "timeweb active"'
# 3. re-point laptop tunnel back
ssh-keyscan -H 72.56.235.40 >> ~/.ssh/known_hosts 2>/dev/null
sudo sed -i 's/158\.220\.127\.161/72.56.235.40/' /etc/systemd/system/ssh-tunnel-vps.service
sudo systemctl daemon-reload && sudo systemctl restart ssh-tunnel-vps
```
Telegram long-poll delivers unacked updates to whichever bot polls next → Timeweb resumes.

**Key rollback invariant:** only ONE bot polls the TG token at a time. F1 quiesces Timeweb (with pgrep gate) strictly before F4 starts Contabo; rollback stops Contabo (with pgrep gate) strictly before restarting Timeweb. Never two pollers → no `Conflict: terminated by other getUpdates`.

---

## What NOT to touch
- Contabo Xray (ports 443/8443/4443) + tinyproxy(18080) — Maxim's prod inbound proxy. Bot needs no inbound ports.
- Timeweb data — read-only during migration, stop+disable only, never delete.
- Ozon MCP + MMO-file — separate task, out of scope.
- RAG model (e5-small) — migrate as-is, no e5-large upgrade now.

## Order-of-operations summary (downtime-minimizing)
A,B,C,D,E run with **old bot LIVE** (zero downtime prep). Downtime starts at **F1** (stop old) and ends at **F4** (Contabo up) — target < 5 min. G verifies. H decommissions only after user OK.

## Risks flagged for Codex
- tar/uid: creating kesha uid=1001 on Contabo before extract → ownership clean. If uid 1001 taken on Contabo → conflict (checked: no such user, /opt empty, likely free — verify).
- `.credentials.json` host portability — unverified until Phase E; login fallback ready.
- mailru/gmail FREEZE.txt may contain non-installable local refs → documented fallbacks.
- sshd on Contabo default may restrict `-R` bind → D3 ensures AllowTcpForwarding.
- `~/.claude.json` may contain absolute paths / machine id → same /opt path mitigates; if trust prompt blocks, `claude` `--dangerously-skip-permissions` or re-trust once interactively.
