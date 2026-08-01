#!/usr/bin/env bash
# Mutation check for the runtime-switch guards (#16 T5).
#
# Rule in force on this project: a green test proves nothing until it has shown
# it can go red. Each guard below is removed in turn; the named tests must fail.
#
# Usage:  bash docs/tasks/16/spikes/mutation_check_t5.sh
set -u
cd "$(dirname "$0")/../../../.." || exit 1

FILE=chat_state.py
BACKUP=$(mktemp)
cp "$FILE" "$BACKUP"
trap 'cp "$BACKUP" "$FILE"; rm -f "$BACKUP"' EXIT

run() { UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest tests/test_runtime_switch.py -q 2>&1 | tail -1; }

echo "== baseline (expect: all pass) =="
run

echo
echo "== mutant A: phase gate removed (switching allowed mid-turn) =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read()
s=s.replace("""            if self.phase is not ChatPhase.IDLE:
                return {"ok": False, "reason": "busy", "phase": str(self.phase),
                        "runtime": target}""", """            if False:
                pass""")
open(p,"w").write(s)
PY
run
cp "$BACKUP" "$FILE"

echo
echo "== mutant B: probe result ignored (broken runtime adopted) =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read()
s=s.replace("""            probe = await self._probe_runtime(new_session)
            if not probe["ok"]:
                raise RuntimeError(probe.get("error") or "runtime did not respond")""",
"""            probe = await self._probe_runtime(new_session)""")
open(p,"w").write(s)
PY
run
cp "$BACKUP" "$FILE"

echo
echo "== mutant C: incumbent disconnected before the probe =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read()
s=s.replace("""            new_session = self._build_runtime(target, self.chat_id)

            probe = await self._probe_runtime(new_session)""",
"""            new_session = self._build_runtime(target, self.chat_id)
            await old_session.safe_disconnect()

            probe = await self._probe_runtime(new_session)""")
open(p,"w").write(s)
PY
run

cp "$BACKUP" "$FILE"

echo
echo "== mutant D: bridge handles not revoked on switch =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read()
s=s.replace("            revoke_chat_sessions(self.chat_id, old_runtime)", "            pass")
open(p,"w").write(s)
PY
run
cp "$BACKUP" "$FILE"

echo
echo "== mutant E: revocation ignores chat scoping (hits other users) =="
BRIDGE_BACKUP=$(mktemp); cp tool_bridge.py "$BRIDGE_BACKUP"
python3 - <<'PY'
p="tool_bridge.py"; s=open(p).read()
s=s.replace("        if cid == chat_id and (not runtime or owner == runtime)",
            "        if (not runtime or owner == runtime)")
open(p,"w").write(s)
PY
run
cp "$BRIDGE_BACKUP" tool_bridge.py; rm -f "$BRIDGE_BACKUP"

echo
echo "== mutant F: post-switch drain removed (reminder stranded) =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read()
s=s.replace("""        # Anything that arrived during the swap (a reminder, a queued message)
        # is drained here rather than dropped.
        await self._drain_or_idle(record_activity=False)""", "        pass")
open(p,"w").write(s)
PY
run

echo
echo "Expected: baseline green; A -> 3; B -> 1; C -> 2; D -> 3; E -> 1; F -> 1."
