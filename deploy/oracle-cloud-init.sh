#!/bin/bash
# Replace placeholders before pasting into Oracle Cloud cloud-init. Never commit real secrets.
set -euo pipefail
REPO_URL="https://github.com/Joel-171/polymarket-5m-paperbot.git"
TELEGRAM_BOT_TOKEN="REPLACE_WITH_BOTFATHER_TOKEN"
TELEGRAM_ALLOWED_USER_ID="REPLACE_WITH_NUMERIC_TELEGRAM_USER_ID"
BOT_INITIAL_CASH="100"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip git
mkdir -p /opt/polymarket-paperbot
git clone "$REPO_URL" /opt/polymarket-paperbot
python3 -m venv /opt/polymarket-paperbot/.venv
/opt/polymarket-paperbot/.venv/bin/pip install -r /opt/polymarket-paperbot/requirements.txt
mkdir -p /opt/polymarket-paperbot/data
cat >/etc/polymarket-paperbot.env <<ENV
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_ALLOWED_USER_ID=${TELEGRAM_ALLOWED_USER_ID}
BOT_INITIAL_CASH=${BOT_INITIAL_CASH}
BOT_DB_PATH=/opt/polymarket-paperbot/data/bot_state.sqlite3
ENV
chmod 600 /etc/polymarket-paperbot.env
cat >/etc/systemd/system/polymarket-paperbot.service <<'UNIT'
[Unit]
Description=Polymarket Telegram Paper Bot
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
WorkingDirectory=/opt/polymarket-paperbot
EnvironmentFile=/etc/polymarket-paperbot.env
ExecStart=/opt/polymarket-paperbot/.venv/bin/python /opt/polymarket-paperbot/service.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now polymarket-paperbot.service
