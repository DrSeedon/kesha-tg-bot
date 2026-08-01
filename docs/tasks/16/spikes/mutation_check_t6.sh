#!/usr/bin/env bash
# Mutation check for compact dispatch (#16 T6).
#
# The dangerous direction is mutant H: routing Claude into native compaction
# would silently disable Kesha's own summarize-and-swap transaction on the
# path that just survived a production incident.
#
# Usage:  bash docs/tasks/16/spikes/mutation_check_t6.sh
set -u
cd "$(dirname "$0")/../../../.." || exit 1

FILE=chat_state.py
BACKUP=$(mktemp)
cp "$FILE" "$BACKUP"
trap 'cp "$BACKUP" "$FILE"; rm -f "$BACKUP"' EXIT

run() { UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest tests/test_compact_dispatch.py -q 2>&1 | tail -1; }

echo "== baseline (expect: all pass) =="
run

echo
echo "== mutant G: native runtimes forced through Kesha's transaction =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read()
s=s.replace("            if self._native_compact:\n                result = await self._do_native_compact()\n            else:",
            "            if False:\n                result = await self._do_native_compact()\n            else:")
open(p,"w").write(s)
PY
run
cp "$BACKUP" "$FILE"

echo
echo "== mutant H: Claude forced into native compaction (the dangerous one) =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read()
s=s.replace("            if self._native_compact:\n                result = await self._do_native_compact()\n            else:",
            "            if True:\n                result = await self._do_native_compact()\n            else:")
open(p,"w").write(s)
PY
run
cp "$BACKUP" "$FILE"

echo
echo "== mutant I: preventive timer left on for native runtimes =="
python3 - <<'PY'
p="chat_state.py"; s=open(p).read()
s=s.replace("        if self._native_compact:\n            # The preventive timer",
            "        if False:\n            # The preventive timer")
open(p,"w").write(s)
PY
run

echo
echo "Expected: baseline green; G -> 5 failures; H -> 2; I -> 1."
