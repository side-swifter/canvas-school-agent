#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"
TARGET_USER="$(id -un)"
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; fi
sed -e "s|REPLACE_REPO_PATH|$ROOT|g" -e "s|REPLACE_USER|$TARGET_USER|g" deploy/school-agent.service > /tmp/school-agent.service
sudo cp /tmp/school-agent.service /etc/systemd/system/school-agent.service
sudo cp deploy/school-agent.timer /etc/systemd/system/school-agent.timer
sudo systemctl daemon-reload
printf '\nInstall ready. Configure .env, test with .venv/bin/school-agent scan, then enable:\n'
printf 'sudo systemctl enable --now school-agent.timer\n'
