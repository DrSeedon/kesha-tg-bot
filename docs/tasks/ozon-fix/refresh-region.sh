#!/bin/bash
# Refresh the Krasnoyarsk region cookies (krsk-state.json) by re-running the capture flow headless.
# capture-region.mjs writes /tmp/krsk-state.json ONLY if it lands in Krasnoyarsk (fail-closed);
# we then atomically install it to the live path. If capture fails, the old state is kept untouched.
#
# Used by: (a) weekly preventive cron, (b) reactive auto-refresh when browser.js self-check fails.
# Must run as user ozon (HOME=/home/ozon for the browser profile). Single-flight via flock.

set -u
DIR=/opt/ozon-mcp-server
STATE="$DIR/krsk-state.json"
TMP=/tmp/krsk-state.json
LOCK=/tmp/ozon-region-refresh.lock

exec 9>"$LOCK"
flock -n 9 || { echo "[refresh-region] another refresh in progress → skip" >&2; exit 0; }

rm -f "$TMP"
echo "[refresh-region] running capture-region.mjs headless…" >&2
cd "$DIR" || exit 1
env HOME=/home/ozon node "$DIR/capture-region.mjs" headless >&2 2>&1

# capture-region.mjs writes /tmp/krsk-state.json ONLY when it confirmed region==Krasnoyarsk
# (fail-closed at its step 8). So: file exists + valid JSON = trustworthy. No city-in-cookies check.
if [ -s "$TMP" ] && node -e "JSON.parse(require('fs').readFileSync('$TMP','utf8'))" 2>/dev/null; then
    # atomic install: valid JSON captured in Krasnoyarsk → replace live state
    cp "$TMP" "$STATE.new" && mv -f "$STATE.new" "$STATE"
    echo "[refresh-region] ✓ installed fresh krsk-state.json" >&2
    exit 0
fi
echo "[refresh-region] ✗ capture did not yield a valid Krasnoyarsk state — keeping old krsk-state.json" >&2
exit 1
