#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EDID_TOOL="${ROOT_DIR}/app/hdmirx_edid.py"
HDMIRX_SYSFS="/sys/class/hdmirx/hdmirx/edid"

choose_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    if [[ "${PYTHON_BIN}" == */* ]]; then
      [[ -x "${PYTHON_BIN}" ]] && { echo "${PYTHON_BIN}"; return 0; }
    elif command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
      echo "${PYTHON_BIN}"
      return 0
    fi
  fi
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    echo "${ROOT_DIR}/.venv/bin/python"
    return 0
  fi
  command -v python3 >/dev/null 2>&1 && { echo "python3"; return 0; }
  echo "python3 not found" >&2
  exit 1
}

PYTHON_BIN="$(choose_python)"

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/hdmirx_edid.sh status
  sudo bash scripts/hdmirx_edid.sh builtin-1080p
  sudo bash scripts/hdmirx_edid.sh builtin-2k
  sudo bash scripts/hdmirx_edid.sh compat
  sudo bash scripts/hdmirx_edid.sh standard-dual
  sudo bash scripts/hdmirx_edid.sh single-1080p120-compat
  sudo bash scripts/hdmirx_edid.sh single-1080p60
  sudo bash scripts/hdmirx_edid.sh single-1080p60-compat
  sudo bash scripts/hdmirx_edid.sh single-1080p90
  sudo bash scripts/hdmirx_edid.sh single-1080p120
  sudo bash scripts/hdmirx_edid.sh single-1440p60

Notes:
  builtin-1080p / builtin-2k use the driver built-in EDID groups.
  compat is an alias of the default profile.
  all custom profiles are maintained in app/hdmirx_edid.py.
  standard-dual is the preferred UI/startup profile.
  it advertises standard 1080p120 first, while also exposing 2k60 as the second source-side choice.
  single-1440p60 is the visible manual fallback profile.
  single-1080p60 / single-1080p60-compat / 90 / 120 are backend-only debug presets and are hidden from the UI.
EOF
}

set_group() {
  local value="$1"
  if [[ ! -e "${HDMIRX_SYSFS}" ]]; then
    echo "HDMI RX EDID sysfs not found: ${HDMIRX_SYSFS}" >&2
    exit 1
  fi
  echo "${value}" > "${HDMIRX_SYSFS}"
  echo "Built-in EDID group switched to ${value}."
  echo "Now replug the HDMI source or make the source redetect the display."
}

main() {
  local cmd="${1:-}"
  case "${cmd}" in
    status)
      exec "${PYTHON_BIN}" "${EDID_TOOL}" status
      ;;
    builtin-1080p)
      [[ $# -eq 1 ]] || { usage; exit 1; }
      set_group 1
      ;;
    builtin-2k)
      [[ $# -eq 1 ]] || { usage; exit 1; }
      set_group 2
      ;;
    compat)
      exec "${PYTHON_BIN}" "${EDID_TOOL}" apply
      ;;
    standard-dual|single-1080p120-compat|single-1080p60|single-1080p60-compat|single-1080p90|single-1080p120|single-1440p60)
      exec "${PYTHON_BIN}" "${EDID_TOOL}" apply "${cmd}"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
