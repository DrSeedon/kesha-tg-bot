#!/usr/bin/env bash
# Mutation check for runtime-aware quota limits (#16 T7).
#
# What these guard: the user must learn WHOSE limit was hit and WHEN it
# resets. Losing either turns a terminal limit back into the silent
# "try later" that cost the user an hour.
#
# Usage:  bash docs/tasks/16/spikes/mutation_check_t7.sh
set -u
cd "$(dirname "$0")/../../../.." || exit 1

FILE=response_stream.py
BACKUP=$(mktemp)
cp "$FILE" "$BACKUP"
trap 'cp "$BACKUP" "$FILE"; rm -f "$BACKUP"' EXIT

run() { UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest tests/test_runtime_limits.py -q 2>&1 | tail -1; }

echo "== baseline (expect: all pass) =="
run

echo
echo "== mutant J: runtime's own reset data ignored (date lost) =="
python3 - <<'PY'
p="response_stream.py"; s=open(p).read()
s=s.replace('        reset = _runtime_limit_suffix(cid) or _session_limit_reset(err) or ""',
            '        reset = _session_limit_reset(err) or ""')
open(p,"w").write(s)
PY
run
cp "$BACKUP" "$FILE"

echo
echo "== mutant K: label hardcoded to Claude (wrong subscription named) =="
python3 - <<'PY'
p="response_stream.py"; s=open(p).read()
s=s.replace('        runtime = _runtime_label(cid) or "Claude"', '        runtime = "Claude"')
open(p,"w").write(s)
PY
run
cp "$BACKUP" "$FILE"

echo
echo "== mutant L: limit retried instead of terminating =="
python3 - <<'PY'
p="response_stream.py"; s=open(p).read()
s=s.replace('                    if chunk.get("kind") == "usage_limit" or reset is not None:',
            '                    if False:')
open(p,"w").write(s)
PY
run

echo
echo "Expected: baseline green; J -> 1 failure; K -> 1; L -> 5."
