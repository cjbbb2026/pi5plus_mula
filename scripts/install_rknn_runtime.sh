#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-install}"
LIB_SRC="${SCRIPT_DIR}/librknnrt.so"
SERVER_SRC="${SCRIPT_DIR}/rknn_server"
LIB_DST="/usr/lib/librknnrt.so"
SERVER_DST="/usr/bin/rknn_server"

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/install_rknn_runtime.sh install
  sudo bash scripts/install_rknn_runtime.sh status
EOF
}

need_root() {
  if [[ ${EUID} -eq 0 ]]; then
    return 0
  fi
  exec sudo bash "$0" "${ACTION}"
}

show_status() {
  echo "Source files:"
  [[ -f "${LIB_SRC}" ]] && ls -l "${LIB_SRC}" || echo "missing: ${LIB_SRC}"
  [[ -f "${SERVER_SRC}" ]] && ls -l "${SERVER_SRC}" || echo "missing: ${SERVER_SRC}"
  echo
  echo "Installed files:"
  [[ -f "${LIB_DST}" ]] && ls -l "${LIB_DST}" || echo "missing: ${LIB_DST}"
  [[ -f "${SERVER_DST}" ]] && ls -l "${SERVER_DST}" || echo "missing: ${SERVER_DST}"
  echo
  if [[ -f "${LIB_DST}" ]]; then
    echo "Runtime version strings:"
    strings "${LIB_DST}" 2>/dev/null | grep -i "librknnrt version" | head -n 1 || true
  fi
}

install_runtime() {
  need_root

  [[ -f "${LIB_SRC}" ]] || {
    echo "Missing runtime library: ${LIB_SRC}" >&2
    exit 1
  }

  if [[ -f "${LIB_DST}" ]]; then
    cp -a "${LIB_DST}" "${LIB_DST}.bak.$(date +%Y%m%d_%H%M%S)"
  fi
  install -m 0755 "${LIB_SRC}" "${LIB_DST}"

  if [[ -f "${SERVER_SRC}" ]]; then
    if [[ -f "${SERVER_DST}" ]]; then
      cp -a "${SERVER_DST}" "${SERVER_DST}.bak.$(date +%Y%m%d_%H%M%S)"
    fi
    install -m 0755 "${SERVER_SRC}" "${SERVER_DST}"
  fi

  ldconfig
  echo "Installed RKNN runtime files."
  show_status
}

case "${ACTION}" in
  install)
    install_runtime
    ;;
  status)
    show_status
    ;;
  *)
    usage
    exit 1
    ;;
esac
