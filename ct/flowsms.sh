#!/usr/bin/env bash
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)

# Copyright (c) 2026
# Author: Havok
# License: MIT
# Source: https://github.com/havokzero/flow-sms

APP="FlowSMS"
var_tags="${var_tags:-sms,mms,flask,python}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-1024}"
var_disk="${var_disk:-6}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/flowsms ]]; then
    msg_error "No ${APP} installation found!"
    exit
  fi

  msg_info "Updating ${APP}"
  cd /opt/flowsms || exit
  git pull
  /opt/flowsms/venv/bin/pip install -r requirements.txt
  systemctl restart flowsms
  msg_ok "Updated ${APP}"
  exit
}

function install_flowsms() {
  msg_info "Installing dependencies"
  apt-get update
  apt-get install -y curl git python3 python3-venv python3-pip ca-certificates
  msg_ok "Installed dependencies"

  msg_info "Cloning FlowSMS"
  mkdir -p /opt/flowsms
  git clone https://github.com/havokzero/flow-sms.git /opt/flowsms
  msg_ok "Cloned FlowSMS"

  msg_info "Creating virtual environment"
  python3 -m venv /opt/flowsms/venv
  /opt/flowsms/venv/bin/pip install --upgrade pip
  /opt/flowsms/venv/bin/pip install -r /opt/flowsms/requirements.txt
  msg_ok "Virtual environment ready"

  msg_info "Preparing configuration"
  if [[ ! -f /opt/flowsms/settings.json ]]; then
    if [[ -f /opt/flowsms/settings.example.json ]]; then
      cp /opt/flowsms/settings.example.json /opt/flowsms/settings.json
    fi
  fi
  msg_ok "Configuration prepared"

  msg_info "Installing systemd service"
  cat >/etc/systemd/system/flowsms.service <<'EOF'
[Unit]
Description=FlowSMS Webhook and Poller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/flowsms
ExecStart=/opt/flowsms/venv/bin/python /opt/flowsms/main.py
Restart=always
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable -q --now flowsms
  msg_ok "Systemd service installed"
}

start
build_container
description
install_flowsms

msg_ok "Completed successfully!"
echo -e "${INFO}${YW}Access the web UI using:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:8080${CL}"
echo -e "${INFO}${YW}Edit configuration here:${CL}"
echo -e "${TAB}${BGN}/opt/flowsms/settings.json${CL}"