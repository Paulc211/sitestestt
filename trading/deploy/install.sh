#!/usr/bin/env bash
# One-shot deploy to an Ubuntu droplet. Run as root from the trading/ dir:
#   bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/tradebot
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv rsync

echo "==> Creating service user + app dir"
id -u tradebot &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin tradebot
mkdir -p "$APP_DIR"

echo "==> Syncing code to $APP_DIR (state/, logs/, .env preserved)"
rsync -a --delete \
  --exclude '.env' --exclude 'state/' --exclude 'logs/' \
  --exclude '.venv/' --exclude '__pycache__' --exclude '.pytest_cache' \
  "$SRC_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/state" "$APP_DIR/logs"

echo "==> Python venv + deps"
python3.12 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "!!  Created $APP_DIR/.env from example — EDIT IT before starting."
fi
chown -R tradebot:tradebot "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Running risk-manager tests (deploy aborts if they fail)"
cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" -m pytest tests/ -q

echo "==> Installing systemd units"
cp "$APP_DIR"/deploy/tradebot-*.service /etc/systemd/system/
systemctl daemon-reload
for svc in trading watchdog news regime reality dashboard; do
  systemctl enable "tradebot-$svc.service"
done

echo
echo "Done. Next steps:"
echo "  1. nano $APP_DIR/.env            # fill in keys (starts in PAPER mode)"
echo "  2. systemctl start tradebot-watchdog tradebot-trading tradebot-news \\"
echo "       tradebot-regime tradebot-reality tradebot-dashboard"
echo "  3. ssh -L 8899:127.0.0.1:8899 root@<droplet>   # then open http://localhost:8899"
echo "  4. journalctl -u tradebot-trading -f           # tail the trading agent"
