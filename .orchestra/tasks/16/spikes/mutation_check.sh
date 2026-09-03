#!/usr/bin/env bash
# Mutation check for the turn-scoping defenses in codex_session.py.
#
# A green test proves nothing until it has shown it can go red. The first
# version of the /stop regression tests stayed green when the cleanup was
# deleted, because they called `_discard_turn_events` by hand instead of
# driving the public generator.
#
# The fix has TWO independent defenses; each is mutated separately here.
#
# Usage:  bash docs/tasks/16/spikes/mutation_check.sh
set -u
cd "$(dirname "$0")/../../../.." || exit 1

FILE=codex_session.py
BACKUP=$(mktemp)
cp "$FILE" "$BACKUP"
restore() { cp "$BACKUP" "$FILE"; rm -f "$BACKUP"; }
trap restore EXIT

run() { UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest tests/test_codex_session.py -q 2>&1 | tail -1; }

echo "== baseline (expect: all pass) =="
run

echo
echo "== mutant 1: drop the finally cleanup (queue grows) =="
python3 - <<'PY'
p = "codex_session.py"
s = open(p).read()
s = s.replace("            self._discard_turn_events(turn_id)",
              "            pass  # MUTANT 1")
open(p, "w").write(s)
PY
run
cp "$BACKUP" "$FILE"

echo
echo "== mutant 2: drop the turnId filter (stale events are consumed) =="
python3 - <<'PY'
p = "codex_session.py"
s = open(p).read()
s = s.replace("""            if turn_id and method != "_process/exited":
                event_turn = params.get("turnId") or ((params.get("turn") or {}).get("id"))
                if event_turn and event_turn != turn_id:
                    continue""",
              """            if False:  # MUTANT 2
                pass""")
open(p, "w").write(s)
PY
run

echo
echo "Expected: baseline green, mutant 1 -> 1 failure, mutant 2 -> 2 failures."
