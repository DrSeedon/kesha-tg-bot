#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${KESHA_BASE_DIR:-/opt/kesha-bot}"
SERVICE_NAME="${KESHA_SERVICE_NAME:-kesha-bot-vps.service}"
CUR_LINK="$BASE_DIR/current"
PREV_LINK="$BASE_DIR/previous"
GUARD_FILE="${KESHA_ROLLBACK_GUARD_FILE:-$BASE_DIR/storage/.last_rollback_guard}"

if [ ! -e "$PREV_LINK" ]; then
  echo "ROLLBACK_ABORT: previous symlink missing" >&2
  exit 1
fi

CUR_TARGET=$(readlink -f "$CUR_LINK" || true)
PREV_TARGET=$(readlink -f "$PREV_LINK" || true)

if [ -z "$PREV_TARGET" ] || [ ! -d "$PREV_TARGET" ]; then
  echo "ROLLBACK_ABORT: previous release directory missing ($PREV_TARGET)" >&2
  exit 1
fi

if [ "$CUR_TARGET" = "$PREV_TARGET" ]; then
  echo "ROLLBACK_ABORT: current == previous ($CUR_TARGET), loop guard triggered" >&2
  exit 0
fi

NOW_TS=$(date +%s)
if [ -f "$GUARD_FILE" ]; then
  LAST_ENTRY=$(cat "$GUARD_FILE" || true)
  LAST_TARGET="${LAST_ENTRY%%|*}"
  LAST_TS="${LAST_ENTRY##*|}"
  if [ "$LAST_TARGET" = "$PREV_TARGET" ] && [ -n "$LAST_TS" ] && [ $((NOW_TS - LAST_TS)) -lt 120 ]; then
    echo "ROLLBACK_ABORT: cooldown active for $PREV_TARGET" >&2
    exit 0
  fi
fi

mkdir -p "$(dirname "$GUARD_FILE")"
printf '%s|%s
' "$PREV_TARGET" "$NOW_TS" > "$GUARD_FILE"

ln -sfn "$PREV_TARGET" "$BASE_DIR/.current.new"
mv -Tf "$BASE_DIR/.current.new" "$CUR_LINK"
echo "ROLLBACK_SWITCHED current=$PREV_TARGET (was $CUR_TARGET)"

if [ "${KESHA_SKIP_SYSTEMCTL:-0}" != "1" ]; then
  systemctl reset-failed "$SERVICE_NAME" || true
  systemctl start "$SERVICE_NAME"
fi
