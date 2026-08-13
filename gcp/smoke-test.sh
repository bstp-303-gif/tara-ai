#!/usr/bin/env bash
# Post-deploy smoke test for the GPGD app. Run after gcp/deploy.sh to catch a
# broken deploy before real users hit it. Exits non-zero if anything fails,
# so it's safe to chain: ./gcp/deploy.sh && ./gcp/smoke-test.sh
#
# Usage:
#   ./gcp/smoke-test.sh                       # tests the GCP static IP over http
#   ./gcp/smoke-test.sh https://gpgd.example  # tests a specific URL instead
#   SSH_CHECKS=true ./gcp/smoke-test.sh       # also SSH in and check container health
set -uo pipefail

PROJECT_ID="${PROJECT_ID:-prestij-jayanthi-gpgdselector}"
REGION="${REGION:-asia-southeast1}"
ZONE="${ZONE:-asia-southeast1-b}"
VM_NAME="${VM_NAME:-gpgd-app}"
STATIC_IP_NAME="${STATIC_IP_NAME:-gpgd-app-ip}"
SSH_CHECKS="${SSH_CHECKS:-false}"

if [ -n "${1:-}" ]; then
  BASE_URL="$1"
else
  IP="$(gcloud compute addresses describe "$STATIC_IP_NAME" --region="$REGION" --project="$PROJECT_ID" --format='get(address)' 2>/dev/null)"
  if [ -z "$IP" ]; then
    echo "ERROR: could not resolve the static IP and no URL was given. Pass one explicitly." >&2
    exit 1
  fi
  BASE_URL="http://$IP"
fi

PASS=0
FAIL=0
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

check() {
  local desc="$1" url="$2" expect="$3"
  local code
  code="$(curl -s -o "$BODY_FILE" -w '%{http_code}' -L --max-time 10 "$url" 2>/dev/null)"
  code="${code:-000}"
  if echo "$expect" | grep -qw "$code"; then
    echo "PASS  [$code] $desc"
    PASS=$((PASS + 1))
  else
    echo "FAIL  [$code, expected $expect] $desc"
    FAIL=$((FAIL + 1))
  fi
}

echo "==> Smoke testing $BASE_URL"
echo

check "Root loads (redirect chain resolves to login)" "$BASE_URL/" "200"
check "Login page loads"                              "$BASE_URL/agents/login/" "200"
check "Admin login page loads"                        "$BASE_URL/admin/" "200"
check "Static files served (whitenoise + collectstatic ran)" "$BASE_URL/static/admin/css/base.css" "200"

echo
if grep -qi "DisallowedHost\|Traceback (most recent call last)" "$BODY_FILE" 2>/dev/null; then
  echo "FAIL  Last response body looks like a Django debug/error page (check DEBUG/ALLOWED_HOSTS)"
  FAIL=$((FAIL + 1))
else
  echo "PASS  No Django debug/traceback leakage detected"
  PASS=$((PASS + 1))
fi

if [ "$SSH_CHECKS" = "true" ]; then
  echo
  echo "==> Checking container health on $VM_NAME via SSH"
  REMOTE_OUT="$(gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap --project="$PROJECT_ID" --command='
    cd /opt/gpgd &&
    echo "--- docker compose ps ---" &&
    sudo docker compose ps &&
    echo "--- unapplied migrations ---" &&
    sudo docker compose exec -T web python manage.py showmigrations --plan | grep -c "\[ \]" || true
  ' 2>&1)"
  echo "$REMOTE_OUT"
  if echo "$REMOTE_OUT" | grep -qi "Up\|running"; then
    echo "PASS  web container is running"
    PASS=$((PASS + 1))
  else
    echo "FAIL  web container does not appear to be running"
    FAIL=$((FAIL + 1))
  fi
fi

echo
echo "==> $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
