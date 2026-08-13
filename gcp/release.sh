#!/usr/bin/env bash
# Full deployment pipeline: ensure infra exists (idempotent), back up the
# current release, deploy the new code, smoke test it, and automatically
# roll back to the last known-good release if the smoke test fails.
#
# This is the one command to run for a real deploy. gcp/setup.sh,
# gcp/deploy.sh, and gcp/smoke-test.sh remain usable standalone for faster
# iterate-and-check cycles during development.
#
# Usage:
#   ./gcp/release.sh
set -uo pipefail

PROJECT_ID="${PROJECT_ID:-prestij-jayanthi-gpgdselector}"
ZONE="${ZONE:-asia-southeast1-b}"
VM_NAME="${VM_NAME:-gpgd-app}"
REMOTE_DIR="/opt/gpgd"
BACKUP_DIR="/opt/gpgd.previous"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail() { echo "ERROR: $1" >&2; exit 1; }

echo "=========================================="
echo "STEP 1/4 — Ensure infra exists (idempotent)"
echo "=========================================="
bash "$SCRIPT_DIR/setup.sh" || fail "setup.sh failed — infra isn't ready, aborting before touching the current deploy"

echo
echo "=========================================="
echo "STEP 2/4 — Back up current known-good release"
echo "=========================================="
gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap --project="$PROJECT_ID" --command="
  if [ -d $REMOTE_DIR ] && (cd $REMOTE_DIR && sudo docker compose ps --status running 2>/dev/null | grep -q web); then
    echo 'Current deployment is running — backing it up to $BACKUP_DIR'
    sudo rm -rf $BACKUP_DIR
    sudo cp -a $REMOTE_DIR $BACKUP_DIR
  else
    echo 'No running deployment found to back up (first deploy, or previous deploy was already broken)'
  fi
" || echo "WARNING: could not confirm/back up the previous release — continuing, but there'll be nothing to roll back to if this deploy fails"

echo
echo "=========================================="
echo "STEP 3/4 — Deploy"
echo "=========================================="
bash "$SCRIPT_DIR/deploy.sh" || fail "deploy.sh failed outright (containers likely never started) — check the output above"

echo
echo "=========================================="
echo "STEP 4/4 — Smoke test"
echo "=========================================="
if bash "$SCRIPT_DIR/smoke-test.sh"; then
  echo
  echo "==> Release succeeded and passed smoke tests."
  exit 0
fi

echo
echo "!! Smoke test FAILED after deploy. Rolling back to the previous release..."

gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap --project="$PROJECT_ID" --command="
  if [ -d $BACKUP_DIR ]; then
    cd $REMOTE_DIR && sudo docker compose down
    sudo rm -rf $REMOTE_DIR
    sudo mv $BACKUP_DIR $REMOTE_DIR
    cd $REMOTE_DIR && sudo docker compose up --build -d
  else
    echo 'No previous release to roll back to — service is left as-is for you to investigate.'
    exit 1
  fi
"
ROLLBACK_STATUS=$?

if [ "$ROLLBACK_STATUS" -ne 0 ]; then
  fail "Rollback could not run (no previous release, or SSH failed) — the broken deploy is still live. Investigate manually."
fi

echo "==> Rolled back. Verifying the rollback is healthy..."
if bash "$SCRIPT_DIR/smoke-test.sh"; then
  echo "==> Rollback confirmed healthy. The new release was NOT deployed — fix the issue and try again."
  exit 1
else
  fail "Rollback also failed its smoke test. The VM may be in a broken state — investigate manually via SSH."
fi
