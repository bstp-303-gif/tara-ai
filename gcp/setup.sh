#!/usr/bin/env bash
# Provisions the GCP infrastructure for the GPGD Django app: a single
# Compute Engine VM with a static IP and firewall rules for HTTP/HTTPS plus
# SSH-via-IAP only (no open port 22). Matches the SQLite-on-disk design in
# ../docker-compose.yml — everything lives on the VM's boot disk.
#
# Safe to re-run: every step checks whether the resource already exists first.
#
# Usage:
#   ./gcp/setup.sh
# Override any default via env var, e.g.:
#   MACHINE_TYPE=e2-small ./gcp/setup.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-prestij-jayanthi-gpgdselector}"
REGION="${REGION:-asia-southeast1}"
ZONE="${ZONE:-asia-southeast1-b}"
VM_NAME="${VM_NAME:-gpgd-app}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-medium}"
BOOT_DISK_SIZE="${BOOT_DISK_SIZE:-30GB}"
STATIC_IP_NAME="${STATIC_IP_NAME:-gpgd-app-ip}"
NETWORK_NAME="${NETWORK_NAME:-gpgd-vpc}"
SUBNET_NAME="${SUBNET_NAME:-gpgd-subnet}"
SUBNET_RANGE="${SUBNET_RANGE:-10.10.0.0/24}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Project: $PROJECT_ID | Region: $REGION | Zone: $ZONE"
gcloud config set project "$PROJECT_ID" --quiet

echo "==> Enabling required APIs (compute, IAP for SSH tunneling)"
gcloud services enable compute.googleapis.com iap.googleapis.com --quiet

echo "==> VPC network: $NETWORK_NAME"
if ! gcloud compute networks describe "$NETWORK_NAME" &>/dev/null; then
  gcloud compute networks create "$NETWORK_NAME" --subnet-mode=custom
else
  echo "    already exists, skipping"
fi

echo "==> Subnet: $SUBNET_NAME ($SUBNET_RANGE in $REGION)"
if ! gcloud compute networks subnets describe "$SUBNET_NAME" --region="$REGION" &>/dev/null; then
  gcloud compute networks subnets create "$SUBNET_NAME" \
    --network="$NETWORK_NAME" --region="$REGION" --range="$SUBNET_RANGE"
else
  echo "    already exists, skipping"
fi

echo "==> Reserving static external IP: $STATIC_IP_NAME"
if ! gcloud compute addresses describe "$STATIC_IP_NAME" --region="$REGION" &>/dev/null; then
  gcloud compute addresses create "$STATIC_IP_NAME" --region="$REGION"
else
  echo "    already exists, skipping"
fi
STATIC_IP="$(gcloud compute addresses describe "$STATIC_IP_NAME" --region="$REGION" --format='get(address)')"

echo "==> Firewall: allow HTTP (80) and HTTPS (443) from anywhere"
if ! gcloud compute firewall-rules describe gpgd-allow-http &>/dev/null; then
  gcloud compute firewall-rules create gpgd-allow-http \
    --network="$NETWORK_NAME" --direction=INGRESS --action=ALLOW \
    --rules=tcp:80 --source-ranges=0.0.0.0/0 --target-tags=gpgd-app
else
  echo "    gpgd-allow-http already exists, skipping"
fi
if ! gcloud compute firewall-rules describe gpgd-allow-https &>/dev/null; then
  gcloud compute firewall-rules create gpgd-allow-https \
    --network="$NETWORK_NAME" --direction=INGRESS --action=ALLOW \
    --rules=tcp:443 --source-ranges=0.0.0.0/0 --target-tags=gpgd-app
else
  echo "    gpgd-allow-https already exists, skipping"
fi

echo "==> Firewall: allow SSH (22) only from Google's Identity-Aware Proxy range"
if ! gcloud compute firewall-rules describe gpgd-allow-ssh-iap &>/dev/null; then
  gcloud compute firewall-rules create gpgd-allow-ssh-iap \
    --network="$NETWORK_NAME" --direction=INGRESS --action=ALLOW \
    --rules=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=gpgd-app
else
  echo "    gpgd-allow-ssh-iap already exists, skipping"
fi

echo "==> Creating VM: $VM_NAME"
if ! gcloud compute instances describe "$VM_NAME" --zone="$ZONE" &>/dev/null; then
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size="$BOOT_DISK_SIZE" \
    --boot-disk-type=pd-balanced \
    --tags=gpgd-app \
    --network="$NETWORK_NAME" \
    --subnet="$SUBNET_NAME" \
    --address="$STATIC_IP" \
    --metadata-from-file=startup-script="$SCRIPT_DIR/startup-script.sh"
else
  echo "    $VM_NAME already exists, skipping create"
fi

cat <<EOF

==> Done.

Static IP:       $STATIC_IP
SSH (via IAP):   gcloud compute ssh $VM_NAME --zone=$ZONE --tunnel-through-iap --project=$PROJECT_ID
Firewall allows: 80, 443 from anywhere; 22 only from Google's IAP range (35.235.240.0/20)

Next steps:
  1. Point your domain's DNS A record at $STATIC_IP (or use the IP directly for now).
  2. In your local .env, set DJANGO_ALLOWED_HOSTS and GPGD_SITE_URL to match that domain/IP.
  3. Wait ~1-2 min for the startup script to finish installing Docker on first boot.
  4. Run ./gcp/deploy.sh to push the app code and start it.
EOF
