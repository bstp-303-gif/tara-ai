#!/usr/bin/env bash
# Pushes the current project code to the GCP VM created by ./gcp/setup.sh
# and (re)starts it via docker compose. Run setup.sh once before this.
#
# Usage:
#   ./gcp/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-prestij-jayanthi-gpgdselector}"
ZONE="${ZONE:-asia-southeast1-b}"
VM_NAME="${VM_NAME:-gpgd-app}"
REMOTE_DIR="/opt/gpgd"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "$SCRIPT_DIR/.env.production" ]; then
  echo "ERROR: $SCRIPT_DIR/.env.production not found. Fill it in (see the template) before deploying." >&2
  exit 1
fi
if grep -q '^DJANGO_SECRET_KEY=$' "$SCRIPT_DIR/.env.production" || grep -q '^DJANGO_DEBUG=True' "$SCRIPT_DIR/.env.production"; then
  echo "ERROR: .env.production still has an empty DJANGO_SECRET_KEY or DJANGO_DEBUG=True. Fill it in for real before deploying." >&2
  exit 1
fi

echo "==> Syncing project files to $VM_NAME:/tmp/gpgd-deploy"
gcloud compute scp --recurse --zone="$ZONE" --tunnel-through-iap --project="$PROJECT_ID" \
  "$SCRIPT_DIR/Dockerfile" \
  "$SCRIPT_DIR/docker-compose.yml" \
  "$SCRIPT_DIR/Caddyfile" \
  "$SCRIPT_DIR/entrypoint.sh" \
  "$SCRIPT_DIR/requirements.txt" \
  "$SCRIPT_DIR/manage.py" \
  "$SCRIPT_DIR/mygpgd" \
  "$SCRIPT_DIR/agents" \
  "$SCRIPT_DIR/.env.production" \
  "$VM_NAME":/tmp/gpgd-deploy/

echo "==> Moving into place and starting containers"
gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap --project="$PROJECT_ID" --command="
  sudo mkdir -p $REMOTE_DIR &&
  sudo rsync -a --delete /tmp/gpgd-deploy/ $REMOTE_DIR/ &&
  sudo mv $REMOTE_DIR/.env.production $REMOTE_DIR/.env &&
  cd $REMOTE_DIR &&
  sudo docker compose up --build -d &&
  sudo docker compose ps
"

cat <<EOF

==> Deployed.

Tail logs:
  gcloud compute ssh $VM_NAME --zone=$ZONE --tunnel-through-iap --project=$PROJECT_ID --command="cd $REMOTE_DIR && sudo docker compose logs -f"
EOF
