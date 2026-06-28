#!/usr/bin/env bash
set -euo pipefail

HOST="hetzner"
USER="hetzner"
REMOTE_DIR="/home/hetzner/AI/blue-panda-email-service"

echo "==> Syncing code to ${USER}@${HOST}:${REMOTE_DIR}"
rsync -avz --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='.pytest_cache' \
  -e ssh \
  "$(dirname "$0")/../" "${USER}@${HOST}:${REMOTE_DIR}/"

echo "==> Creating venv and installing deps on ${HOST}"
ssh "${USER}@${HOST}" "cd ${REMOTE_DIR} && python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e ."

echo "==> Installing systemd service"
ssh "${USER}@${HOST}" "sudo cp ${REMOTE_DIR}/launchd/gmail-service.service /etc/systemd/system/gmail-service.service && sudo systemctl daemon-reload"

echo "==> Creating token directory"
ssh "${USER}@${HOST}" "mkdir -p /home/hetzner/.gmail-service && chmod 700 /home/hetzner/.gmail-service"

echo "==> Deployment complete. Next steps:"
echo "    1. Place credentials.json in /home/hetzner/.gmail-service/"
echo "    2. Run OAuth flow AS focusedbluepanda@gmail.com (never brunobarrientosf@gmail.com):"
echo "       ssh ${USER}@${HOST} 'cd ${REMOTE_DIR} && .venv/bin/python -c \"from gmail_service.auth import run_oauth_flow; from gmail_service.config import Settings; run_oauth_flow(Settings.from_env())\"'"
echo "    3. Verify /profile shows focusedbluepanda@gmail.com"
echo "    4. Start service: ssh ${USER}@${HOST} 'sudo systemctl enable --now gmail-service'"
