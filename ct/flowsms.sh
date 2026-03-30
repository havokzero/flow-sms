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
  cd /opt/flowsms || exit 1
  git pull
  /opt/flowsms/venv/bin/pip install --upgrade pip
  /opt/flowsms/venv/bin/pip install -r requirements.txt

  if [[ -f /opt/flowsms/flowsms.service ]]; then
    cp /opt/flowsms/flowsms.service /etc/systemd/system/flowsms.service
    systemctl daemon-reload
  fi

  systemctl restart flowsms
  msg_ok "Updated ${APP}"
  exit
}

function install_flowsms() {
  msg_info "Running FlowSMS installer"
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/havokzero/flow-sms/master/install.sh)"
  msg_ok "Installed ${APP}"
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