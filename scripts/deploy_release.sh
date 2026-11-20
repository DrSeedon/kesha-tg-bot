#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${KESHA_BASE_DIR:-/opt/kesha-bot}"
REPO_DIR="${KESHA_REPO_DIR:-$BASE_DIR}"
VENV_PYTHON="${KESHA_VENV_PYTHON:-$BASE_DIR/.venv/bin/python3}"
TARGET_REF="${1:-HEAD}"
SERVICE_NAME="${KESHA_SERVICE_NAME:-kesha-bot-vps.service}"

cd "$REPO_DIR"
SHA=$(git rev-parse --short "$TARGET_REF")
RELEASE_DIR="$BASE_DIR/releases/$SHA"
mkdir -p "$BASE_DIR/releases"

TMP_DIR=$(mktemp -d "$BASE_DIR/releases/.staging-$SHA-XXXXXX")
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

git archive "$TARGET_REF" | tar -x -C "$TMP_DIR"
ln -sfn "$BASE_DIR/.env" "$TMP_DIR/.env"
ln -sfn "$BASE_DIR/storage" "$TMP_DIR/storage"
ln -sfn "$BASE_DIR/logs" "$TMP_DIR/logs"
ln -sfn "$BASE_DIR/.venv" "$TMP_DIR/.venv"
if [ -d "$BASE_DIR/artifacts" ]; then
  ln -sfn "$BASE_DIR/artifacts" "$TMP_DIR/artifacts"
fi

"$VENV_PYTHON" "$TMP_DIR/scripts/smoke_imports.py"

rm -rf "$RELEASE_DIR"
mv "$TMP_DIR" "$RELEASE_DIR"
trap - EXIT

if [ -L "$BASE_DIR/current" ] || [ -e "$BASE_DIR/current" ]; then
  OLD_TARGET=$(readlink -f "$BASE_DIR/current" || true)
  if [ -n "$OLD_TARGET" ] && [ "$OLD_TARGET" != "$RELEASE_DIR" ] && [ -d "$OLD_TARGET" ]; then
    ln -sfn "$OLD_TARGET" "$BASE_DIR/.previous.new"
    mv -Tf "$BASE_DIR/.previous.new" "$BASE_DIR/previous"
  fi
fi

ln -sfn "$RELEASE_DIR" "$BASE_DIR/.current.new"
mv -Tf "$BASE_DIR/.current.new" "$BASE_DIR/current"

CUR_KEEP=$(readlink -f "$BASE_DIR/current" || true)
PREV_KEEP=$(readlink -f "$BASE_DIR/previous" || true)
mapfile -t RELS < <(ls -1dt "$BASE_DIR/releases"/*/ 2>/dev/null || true)
KEEP_COUNT=0
for dir in "${RELS[@]}"; do
  dir="${dir%/}"
  KEEP_COUNT=$((KEEP_COUNT + 1))
  if [ "$KEEP_COUNT" -le 5 ] || [ "$dir" = "$CUR_KEEP" ] || [ "$dir" = "$PREV_KEEP" ]; then
    continue
  fi
  rm -rf "$dir"
done

rm -f "$BASE_DIR/storage/greet_on_restart"
if [ "${KESHA_SKIP_RESTART:-0}" != "1" ]; then
  sudo -n systemctl restart "$SERVICE_NAME"
  for _ in $(seq 1 30); do
    if [ "$(systemctl is-active "$SERVICE_NAME" || true)" = "active" ]; then
      echo "DEPLOY_RELEASE_OK sha=$SHA release=$RELEASE_DIR"
      exit 0
    fi
    sleep 1
  done
  echo "DEPLOY_RELEASE_FAILED_ACTIVE sha=$SHA" >&2
  exit 1
fi
echo "DEPLOY_RELEASE_STAGED sha=$SHA release=$RELEASE_DIR"
