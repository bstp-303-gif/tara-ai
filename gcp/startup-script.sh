#!/bin/bash
# GCE startup-script: runs once as root on first boot (and on every reboot,
# but the guards below make re-runs a no-op).
set -euo pipefail

apt-get update
apt-get install -y ca-certificates curl rsync

if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

systemctl enable docker
systemctl start docker

mkdir -p /opt/gpgd
