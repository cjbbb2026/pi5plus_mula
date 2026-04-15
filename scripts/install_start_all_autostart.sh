#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
START_ALL_SH="${SCRIPT_DIR}/start_all.sh"
SERVICE_NAME="${SERVICE_NAME:-orangepic-start-all.service}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
APP_USER="${APP_USER:-${SUDO_USER:-orangepi}}"
APP_HOME="${APP_HOME:-$(getent passwd "${APP_USER}" | cut -d: -f6 || true)}"
ACTION="${1:-install}"

if [[ -z "${APP_HOME}" ]]; then
  APP_HOME="/home/${APP_USER}"
fi

need_root() {
  if [[ ${EUID} -eq 0 ]]; then
    return 0
  fi
  exec sudo APP_USER="${APP_USER}" APP_HOME="${APP_HOME}" SERVICE_NAME="${SERVICE_NAME}" bash "$0" "${ACTION}"
}

render_service() {
  cat <<EOF
[Unit]
Description=OrangePi start_all bootstrap
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${PROJECT_DIR}
Environment=ORIGINAL_USER=${APP_USER}
Environment=ORIGINAL_HOME=${APP_HOME}
ExecStart=/usr/bin/bash ${START_ALL_SH} start
ExecStop=/usr/bin/bash ${START_ALL_SH} stop
ExecReload=/usr/bin/bash ${START_ALL_SH} restart
TimeoutStartSec=120
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
}

install_service() {
  need_root
  render_service > "${SERVICE_FILE}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  echo "Installed: ${SERVICE_FILE}"
  echo "Enabled: ${SERVICE_NAME}"
  echo "User: ${APP_USER}"
  echo "Home: ${APP_HOME}"
  echo "Use 'systemctl start ${SERVICE_NAME}' to start now."
}

uninstall_service() {
  need_root
  systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "${SERVICE_FILE}"
  systemctl daemon-reload
  echo "Removed: ${SERVICE_FILE}"
}

status_service() {
  if [[ -f "${SERVICE_FILE}" ]]; then
    echo "Service file: ${SERVICE_FILE}"
    echo
    cat "${SERVICE_FILE}"
    echo
  else
    echo "Service file missing: ${SERVICE_FILE}"
  fi
  systemctl status "${SERVICE_NAME}" --no-pager || true
}

usage() {
  cat <<EOF
Usage:
  bash scripts/install_start_all_autostart.sh install
  bash scripts/install_start_all_autostart.sh uninstall
  bash scripts/install_start_all_autostart.sh status

Optional env:
  APP_USER=orangepi
  APP_HOME=/home/orangepi
  SERVICE_NAME=orangepic-start-all.service
EOF
}

case "${ACTION}" in
  install)
    install_service
    ;;
  uninstall|remove)
    uninstall_service
    ;;
  status)
    status_service
    ;;
  *)
    usage
    exit 1
    ;;
esac
