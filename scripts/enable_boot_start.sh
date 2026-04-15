#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="${SCRIPT_DIR}/install_start_all_autostart.sh"
SERVICE_NAME="${SERVICE_NAME:-orangepic-start-all.service}"
APP_USER="${APP_USER:-${SUDO_USER:-orangepi}}"
APP_HOME="${APP_HOME:-$(getent passwd "${APP_USER}" | cut -d: -f6 || true)}"

if [[ -z "${APP_HOME}" ]]; then
  APP_HOME="/home/${APP_USER}"
fi

if [[ ! -f "${INSTALLER}" ]]; then
  echo "Installer not found: ${INSTALLER}" >&2
  exit 1
fi

if [[ ${EUID} -ne 0 ]]; then
  exec sudo APP_USER="${APP_USER}" APP_HOME="${APP_HOME}" SERVICE_NAME="${SERVICE_NAME}" bash "$0"
fi

echo "Installing boot autostart service..."
APP_USER="${APP_USER}" APP_HOME="${APP_HOME}" SERVICE_NAME="${SERVICE_NAME}" bash "${INSTALLER}" install

echo "Starting service now..."
systemctl start "${SERVICE_NAME}"

echo "Boot autostart is ready."
systemctl status "${SERVICE_NAME}" --no-pager || true
