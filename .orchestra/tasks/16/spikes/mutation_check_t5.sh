#!/usr/bin/env bash
# Mutation check for the runtime-switch guards (#16 T5).
#
# Rule in force on this project: a green test proves nothing until it has shown
# it can go red. Each guard below is removed in turn; the named tests must fail.
#
# Usage:  bash docs/tasks/16/spikes/mutation_check_t5.sh
set -eu
cd "$(dirname "$0")/../../../.." || exit 1

FILE=chat_state.py
BACKUP=$(mktemp)
cp "$FILE" "$BACKUP"
BRIDGE_BACKUP=
trap 'cp "$BACKUP" "$FILE"; rm -f "$BACKUP"; if [[ -n "$BRIDGE_BACKUP" && -f "$BRIDGE_BACKUP" ]]; then cp "$BRIDGE_BACKUP" tool_bridge.py; rm -f "$BRIDGE_BACKUP"; fi' EXIT

run() {
  local log exit_code=0
  log=$(mktemp)
  uv run --isolated --no-project --exclude-newer 2030-01-01 \
    --with-requirements requirements.txt --with mcp==1.28.1 \
    --with claude-agent-sdk==0.2.158 --with pytest --with pytest-asyncio \
    python -m pytest tests/test_runtime_switch.py -q >"$log" 2>&1 || exit_code=$?
  tail -1 "$log"
  rm -f "$log"
  if [[ "$1" == baseline ]]; then
    [[ "$exit_code" == 0 ]]
  else
    [[ "$exit_code" == 1 ]]
  fi
}

echo "== baseline (expect: all pass) =="
run baseline

echo
echo "== mutant A: phase gate removed (switching allowed mid-turn) =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read(); original=s
s=s.replace("""            if self.phase is not ChatPhase.IDLE or self.pending_transcriptions:
                return {"ok": False, "reason": "busy", "phase": str(self.phase),
                        "runtime": target}""", """            if False:
                pass""")
assert s != original
open(p,"w").write(s)
PY
run mutant
cp "$BACKUP" "$FILE"

echo
echo "== mutant B: probe result ignored (broken runtime adopted) =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read(); original=s
s=s.replace("""            probe = await self._probe_runtime(new_session)
            if not probe["ok"]:
                raise RuntimeError(probe.get("error") or "runtime did not respond")""",
"""            probe = await self._probe_runtime(new_session)""")
assert s != original
open(p,"w").write(s)
PY
run mutant
cp "$BACKUP" "$FILE"

echo
echo "== mutant C: incumbent disconnected before the probe =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read(); original=s
s=s.replace("""            new_session = self._build_runtime(target, self.chat_id, **options)

            probe = await self._probe_runtime(new_session)""",
"""            new_session = self._build_runtime(target, self.chat_id, **options)
            await old_session.safe_disconnect()

            probe = await self._probe_runtime(new_session)""")
assert s != original
open(p,"w").write(s)
PY
run mutant

cp "$BACKUP" "$FILE"

echo
echo "== mutant D: bridge handles not revoked on switch =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read(); original=s
s=s.replace("                revoke_chat_sessions(self.chat_id, old_runtime)", "                pass")
assert s != original
open(p,"w").write(s)
PY
run mutant
cp "$BACKUP" "$FILE"

echo
echo "== mutant E: revocation ignores chat scoping (hits other users) =="
BRIDGE_BACKUP=$(mktemp); cp tool_bridge.py "$BRIDGE_BACKUP"
python3 - <<'PY'
p="tool_bridge.py"; s=open(p).read(); original=s
s=s.replace("        if cid == chat_id and (not runtime or owner == runtime)",
            "        if (not runtime or owner == runtime)")
assert s != original
open(p,"w").write(s)
PY
run mutant
cp "$BRIDGE_BACKUP" tool_bridge.py; rm -f "$BRIDGE_BACKUP"

echo
echo "== mutant F: post-switch drain removed (reminder stranded) =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read(); original=s
boundary=s.index("    async def _retire_runtime_and_finish(")
before, target = s[:boundary], s[boundary:]
stmt = "            await self._drain_or_idle(record_activity=False)"
assert stmt in target
s = before + target.replace(stmt, "            pass", 1)
assert s != original
open(p,"w").write(s)
PY
run mutant

echo
echo "Expected: baseline green; every mutant must fail at least one test."
