#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/flowsms"
REPO_URL="https://github.com/havokzero/flow-sms.git"

echo "[*] Installing dependencies..."
apt-get update
apt-get install -y curl git python3 python3-venv python3-pip ca-certificates

echo "[*] Cloning or updating FlowSMS..."
if [[ ! -d "$APP_DIR/.git" ]]; then
  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
else
  cd "$APP_DIR"
  git pull
fi

echo "[*] Creating virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "[*] Preparing configuration..."
if [[ ! -f "$APP_DIR/settings.json" ]]; then
  if [[ -f "$APP_DIR/settings.example.json" ]]; then
    cp "$APP_DIR/settings.example.json" "$APP_DIR/settings.json"
  elif [[ -f "$APP_DIR/settings.json" ]]; then
    echo "[*] Placeholder settings.json already present in repo, leaving as-is"
  fi
fi

echo "[*] Installing systemd service..."
if [[ -f "$APP_DIR/flowsms.service" ]]; then
  cp "$APP_DIR/flowsms.service" /etc/systemd/system/flowsms.service
else
  echo "[!] flowsms.service missing in repo"
  exit 1
fi

systemctl daemon-reload
systemctl enable --now flowsms

echo
echo "[+] FlowSMS installed"
echo "[+] Web UI: http://$(hostname -I | awk '{print $1}'):8080"
echo "[+] Config: /opt/flowsms/settings.json"
echo "[+] Service: systemctl status flowsms"