#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${ENV_FILE:-/boot/orangepiEnv.txt}"
OVERLAY_NAME="${OVERLAY_NAME:-hdmirx}"
ACTION="${1:-enable}"

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/enable_hdmirx_overlay.sh enable
  sudo bash scripts/enable_hdmirx_overlay.sh status

Optional env:
  ENV_FILE=/boot/orangepiEnv.txt
  OVERLAY_NAME=hdmirx
EOF
}

need_root() {
  if [[ ${EUID} -eq 0 ]]; then
    return 0
  fi
  exec sudo ENV_FILE="${ENV_FILE}" OVERLAY_NAME="${OVERLAY_NAME}" bash "$0" "${ACTION}"
}

status_overlay() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "orangepiEnv file not found: ${ENV_FILE}" >&2
    exit 1
  fi
  echo "Env file: ${ENV_FILE}"
  grep -n '^overlay_prefix=' "${ENV_FILE}" || true
  grep -n '^overlays=' "${ENV_FILE}" || true
}

enable_overlay() {
  need_root

  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "orangepiEnv file not found: ${ENV_FILE}" >&2
    exit 1
  fi

  cp -a "${ENV_FILE}" "${ENV_FILE}.bak"

  if grep -Eq "^overlays=.*(^|[[:space:]])${OVERLAY_NAME}([[:space:]]|$)" "${ENV_FILE}"; then
    echo "Overlay already enabled: ${OVERLAY_NAME}"
    status_overlay
    return 0
  fi

  if grep -q '^overlays=' "${ENV_FILE}"; then
    sed -i -E "s/^overlays=(.*)$/overlays=\1 ${OVERLAY_NAME}/" "${ENV_FILE}"
  else
    printf '\noverlays=%s\n' "${OVERLAY_NAME}" >> "${ENV_FILE}"
  fi

  echo "Enabled overlay: ${OVERLAY_NAME}"
  status_overlay
  echo "Reboot is required."
}

case "${ACTION}" in
  enable)
    enable_overlay
    ;;
  status)
    status_overlay
    ;;
  *)
    usage
    exit 1
    ;;
esac
