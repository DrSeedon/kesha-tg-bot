#!/bin/bash
# Kill ORPHANED ozon MCP node processes — those whose parent sshd lost its TCP connection
# (SSH staled on network/timeout; sshd+node lingered; EOF-cleanup never fired → zombie Chromium).
#
# MUST RUN AS ROOT. `ss` only reveals socket→pid for sockets the caller owns; the ozon MCP's
# parent sshd socket is root-owned, so as user `ozon` ss returns 0 pids → EVERY node looks
# orphaned → it would kill LIVE sessions. Verified empirically. Hence: root-only systemd timer,
# NOT the ozon wrapper.
#
# WHY socket-state, not age: the MCP SSH session is LONG-LIVED (persistent .mcp.json `ssh -T`);
# a healthy node is hours old, so age-based killing nukes live sessions. Reliable zombie signal =
# the node's parent sshd has NO ESTABLISHED socket (or node reparented to init, PPID=1).

if [ "$(id -u)" -ne 0 ]; then
    echo "[kill-stale] must run as root (ss needs root to see sshd socket pids) — abort" >&2
    exit 1
fi

ESTAB_PIDS="$(ss -tnHp state established 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u)"

for np in $(pgrep -u ozon -f '/opt/ozon-mcp-server/src/index.js' 2>/dev/null); do
    ppid="$(ps -o ppid= -p "$np" 2>/dev/null | tr -d ' ')"
    [ -z "$ppid" ] && continue
    if [ "$ppid" = "1" ]; then
        echo "[kill-stale] orphan node $np (PPID=1, sshd gone) → kill" >&2
        kill -TERM "$np" 2>/dev/null; sleep 2; kill -KILL "$np" 2>/dev/null
        continue
    fi
    # parent must be an sshd for ozon; if it has no ESTABLISHED socket → SSH transport gone → zombie
    if ! grep -qx "$ppid" <<<"$ESTAB_PIDS"; then
        etimes="$(ps -o etimes= -p "$np" 2>/dev/null | tr -d ' ')"
        # 2h guard: never race a just-forked session whose socket bookkeeping is settling
        if [ -n "$etimes" ] && [ "$etimes" -gt 7200 ]; then
            echo "[kill-stale] node $np: parent sshd $ppid no ESTABLISHED conn, age ${etimes}s → kill" >&2
            kill -TERM "$np" 2>/dev/null; sleep 2; kill -KILL "$np" 2>/dev/null
        fi
    fi
done
exit 0
