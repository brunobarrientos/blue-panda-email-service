#!/usr/bin/env bash
# Promote reviewed origin/main to the existing Star service; credentials stay put.
set -euo pipefail
expected_sha="${1:?Pass the reviewed merged origin/main SHA}"
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 star "bash -s -- $expected_sha" <<'REMOTE'
set -euo pipefail
expected_sha="$1"
cd /home/star/AI/blue-panda-email-service
test -z "$(git status --porcelain)"
test "$(git branch --show-current)" = main
git fetch origin main
test "$(git rev-parse origin/main)" = "$expected_sha"
git merge-base --is-ancestor HEAD "$expected_sha"
git merge --ff-only origin/main
test "$(git rev-parse HEAD)" = "$expected_sha"
.venv/bin/python -m pip install --no-deps -e .
sudo -n install -d -m 755 /etc/systemd/system/gmail-service.service.d
sudo -n install -m 644 launchd/monitoring-digest.conf /etc/systemd/system/gmail-service.service.d/monitoring-digest.conf
sudo -n systemctl daemon-reload
sudo -n systemctl restart gmail-service.service
systemctl is-active gmail-service.service
git rev-parse HEAD
REMOTE
