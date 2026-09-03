#!/bin/bash
# Forced-command wrapper for the ozon SSH user. Launches the Ozon MCP directly (child of sshd)
# so it inherits stdio cleanly AND dies when the SSH session ends (no orphan Chromium).
# RAM is capped at the ozon user slice level (user-<uid>.slice MemoryMax=800M, systemd drop-in)
# so node + all Chromium children can never OOM prod (seedon.ru/CryptoBot). ozon runs ONLY this MCP.
# CRITICAL: nothing to stdout (it is the MCP JSON-RPC wire). All diagnostics -> stderr.

# Sweep Playwright temp dirs orphaned by a previous crashed/killed browser (>60 min old).
# Normal browser.close() self-cleans; this catches SIGKILL/abrupt-SSH-drop leftovers.
/opt/ozon-mcp-server/clean-tmp.sh 2>/dev/null

cd /opt/ozon-mcp-server || exit 1
exec env HOME=/home/ozon node /opt/ozon-mcp-server/src/index.js
