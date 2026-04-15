#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
VENV_PY="${VENV_DIR}/bin/python"
ENABLE_HDMIRX_OVERLAY_SCRIPT="${SCRIPT_DIR}/enable_hdmirx_overlay.sh"
INSTALL_RKNN_RUNTIME_SCRIPT="${SCRIPT_DIR}/install_rknn_runtime.sh"
ACTION="${1:-install}"

APP_USER="${APP_USER:-${SUDO_USER:-${USER:-orangepi}}}"
APP_HOME="${APP_HOME:-$(getent passwd "${APP_USER}" | cut -d: -f6 || true)}"
ENABLE_AUTOSTART="${ENABLE_AUTOSTART:-1}"
START_AFTER_INSTALL="${START_AFTER_INSTALL:-0}"
INSTALL_AI="${INSTALL_AI:-1}"
INSTALL_LOCAL_KERNEL_DEBS="${INSTALL_LOCAL_KERNEL_DEBS:-auto}"
RKNN_WHEEL="${RKNN_WHEEL:-}"
RKNN_PIP_PACKAGE="${RKNN_PIP_PACKAGE:-rknn-toolkit-lite2}"
RKNN_PIP_VERSION="${RKNN_PIP_VERSION:-2.3.2}"
APT_GET="${APT_GET:-apt-get}"

if [[ -z "${APP_HOME}" ]]; then
  APP_HOME="/home/${APP_USER}"
fi

APT_PACKAGES=(
  python3
  python3-pip
  python3-venv
  python3-dev
  python3-setuptools
  python3-wheel
  python3-numpy
  python3-opencv
  python3-evdev
  v4l-utils
  usbutils
  psmisc
  procps
)

APT_OPTIONAL_PACKAGES=(
  gstreamer1.0-tools
  gstreamer1.0-plugins-base
  gstreamer1.0-plugins-good
)

APT_CONFLICT_PACKAGES=(
  gstreamer1.0-plugins-base-dbg
  gstreamer1.0-plugins-good-dbg
)

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/deploy_new_system.sh install
  sudo bash scripts/deploy_new_system.sh verify

Important env:
  APP_USER=orangepi
  APP_HOME=/home/orangepi
  RKNN_WHEEL=/path/to/rknn_toolkit_lite2-*.whl
  INSTALL_AI=1
  ENABLE_AUTOSTART=1
  START_AFTER_INSTALL=0
  INSTALL_LOCAL_KERNEL_DEBS=auto

Examples:
  sudo bash scripts/deploy_new_system.sh install

Notes:
  1. This script installs system dependencies for passthrough, HDMI RX and the web/AI stack.
  2. By default it will try pip install of RKNN Lite first, then fall back to a local wheel if present.
  3. If RKNN Lite still cannot be imported after installation, deployment will fail.
EOF
}

need_root() {
  if [[ ${EUID} -eq 0 ]]; then
    return 0
  fi
  exec sudo \
    APP_USER="${APP_USER}" \
    APP_HOME="${APP_HOME}" \
    ENABLE_AUTOSTART="${ENABLE_AUTOSTART}" \
    START_AFTER_INSTALL="${START_AFTER_INSTALL}" \
    INSTALL_AI="${INSTALL_AI}" \
    INSTALL_LOCAL_KERNEL_DEBS="${INSTALL_LOCAL_KERNEL_DEBS}" \
    RKNN_WHEEL="${RKNN_WHEEL}" \
    RKNN_PIP_PACKAGE="${RKNN_PIP_PACKAGE}" \
    RKNN_PIP_VERSION="${RKNN_PIP_VERSION}" \
    APT_GET="${APT_GET}" \
    bash "$0" "${ACTION}"
}

log() {
  printf '[deploy] %s\n' "$*"
}

require_user_exists() {
  getent passwd "${APP_USER}" >/dev/null 2>&1 || {
    echo "User does not exist: ${APP_USER}" >&2
    exit 1
  }
}

validate_requested_mode() {
  case "${INSTALL_AI}" in
    0|1) ;;
    *)
      echo "INSTALL_AI must be 0 or 1, got: ${INSTALL_AI}" >&2
      exit 1
      ;;
  esac

  case "${ENABLE_AUTOSTART}" in
    0|1) ;;
    *)
      echo "ENABLE_AUTOSTART must be 0 or 1, got: ${ENABLE_AUTOSTART}" >&2
      exit 1
      ;;
  esac

  case "${START_AFTER_INSTALL}" in
    0|1) ;;
    *)
      echo "START_AFTER_INSTALL must be 0 or 1, got: ${START_AFTER_INSTALL}" >&2
      exit 1
      ;;
  esac

  case "${INSTALL_LOCAL_KERNEL_DEBS}" in
    0|1|auto) ;;
    *)
      echo "INSTALL_LOCAL_KERNEL_DEBS must be 0, 1 or auto, got: ${INSTALL_LOCAL_KERNEL_DEBS}" >&2
      exit 1
      ;;
  esac

  if [[ "${INSTALL_AI}" != "1" && "${ENABLE_AUTOSTART}" == "1" ]]; then
    echo "ENABLE_AUTOSTART=1 requires INSTALL_AI=1 because the autostart service uses scripts/start_all.sh" >&2
    exit 1
  fi

  if [[ "${INSTALL_AI}" != "1" && "${START_AFTER_INSTALL}" == "1" ]]; then
    echo "START_AFTER_INSTALL=1 requires INSTALL_AI=1 because it starts scripts/start_all.sh" >&2
    exit 1
  fi
}

python_import_ok() {
  local module="$1"
  "${VENV_PY}" -c "import ${module}" >/dev/null 2>&1
}

detect_local_rknn_wheel() {
  local wheel=""
  wheel="$(find "${PROJECT_DIR}" -maxdepth 3 -type f \( -iname 'rknn_toolkit_lite2*.whl' -o -iname 'rknn-toolkit-lite2*.whl' \) | head -n 1 || true)"
  if [[ -n "${wheel}" ]]; then
    echo "${wheel}"
  fi
}

package_installed() {
  local package_name="$1"
  dpkg-query -W -f='${Status}' "${package_name}" >/dev/null 2>&1
}

warn_held_apt_packages() {
  local held_packages
  held_packages="$(apt-mark showhold 2>/dev/null || true)"
  if [[ -n "${held_packages}" ]]; then
    log "Held apt packages detected. If apt still fails, unhold them manually:"
    printf '%s\n' "${held_packages}"
  fi
}

unhold_requested_apt_packages() {
  local package_names=("$@")
  local held_packages
  local unhold_packages=()
  local package_name
  held_packages="$(apt-mark showhold 2>/dev/null || true)"
  if [[ -z "${held_packages}" ]]; then
    return 0
  fi

  for package_name in "${package_names[@]}"; do
    if grep -Fxq "${package_name}" <<<"${held_packages}"; then
      unhold_packages+=("${package_name}")
    fi
  done

  if (( ${#unhold_packages[@]} == 0 )); then
    return 0
  fi

  log "Unholding requested apt packages: ${unhold_packages[*]}"
  apt-mark unhold "${unhold_packages[@]}"
}

purge_conflicting_apt_packages() {
  local purge_packages=()
  local package_name
  for package_name in "${APT_CONFLICT_PACKAGES[@]}"; do
    if package_installed "${package_name}"; then
      purge_packages+=("${package_name}")
    fi
  done

  if (( ${#purge_packages[@]} == 0 )); then
    return 0
  fi

  log "Purging conflicting apt debug packages: ${purge_packages[*]}"
  apt-mark unhold "${purge_packages[@]}" >/dev/null 2>&1 || true
  if DEBIAN_FRONTEND=noninteractive "${APT_GET}" purge -y "${purge_packages[@]}"; then
    return 0
  fi

  log "apt purge failed; forcing dpkg purge for debug packages only"
  dpkg --purge --force-depends "${purge_packages[@]}"
}

repair_apt_dependencies() {
  log "Repairing broken apt dependencies"
  DEBIAN_FRONTEND=noninteractive "${APT_GET}" --fix-broken install -y
}

install_apt_packages() {
  log "Updating apt package index"
  "${APT_GET}" update

  warn_held_apt_packages
  purge_conflicting_apt_packages
  if ! repair_apt_dependencies; then
    log "Initial apt dependency repair failed; continuing to package install retry path"
  fi

  log "Installing required system packages"
  unhold_requested_apt_packages "${APT_PACKAGES[@]}"
  if DEBIAN_FRONTEND=noninteractive "${APT_GET}" install -y --allow-change-held-packages "${APT_PACKAGES[@]}"; then
    :
  else
    log "Required package install failed; retrying after GStreamer debug cleanup and apt repair"
    purge_conflicting_apt_packages
    repair_apt_dependencies
    "${APT_GET}" update
    unhold_requested_apt_packages "${APT_PACKAGES[@]}"
    DEBIAN_FRONTEND=noninteractive "${APT_GET}" install -y --allow-change-held-packages "${APT_PACKAGES[@]}"
  fi

  log "Installing optional GStreamer packages"
  unhold_requested_apt_packages "${APT_OPTIONAL_PACKAGES[@]}"
  if DEBIAN_FRONTEND=noninteractive "${APT_GET}" install -y --allow-change-held-packages "${APT_OPTIONAL_PACKAGES[@]}"; then
    return 0
  fi

  log "Optional GStreamer install failed; retrying after apt repair"
  purge_conflicting_apt_packages
  repair_apt_dependencies || true
  unhold_requested_apt_packages "${APT_OPTIONAL_PACKAGES[@]}"
  if DEBIAN_FRONTEND=noninteractive "${APT_GET}" install -y --allow-change-held-packages "${APT_OPTIONAL_PACKAGES[@]}"; then
    return 0
  fi

  log "Optional GStreamer packages still failed. Continuing because v4l2-raw is the default capture backend."
}

install_local_kernel_debs_if_needed() {
  local debs=()
  local image_deb="${SCRIPT_DIR}/linux-image-current-rockchip-rk3588_1.2.2_arm64.deb"
  local dtb_deb="${SCRIPT_DIR}/linux-dtb-current-rockchip-rk3588_1.2.2_arm64.deb"
  [[ -f "${image_deb}" ]] && debs+=("${image_deb}")
  [[ -f "${dtb_deb}" ]] && debs+=("${dtb_deb}")

  if [[ "${INSTALL_LOCAL_KERNEL_DEBS}" == "0" ]]; then
    log "Skipping local RK3588 kernel packages"
    return 0
  fi

  if [[ "${INSTALL_LOCAL_KERNEL_DEBS}" == "auto" && ${#debs[@]} -eq 0 ]]; then
    log "No bundled RK3588 kernel packages found, skip local kernel install"
    return 0
  fi

  if (( ${#debs[@]} == 0 )); then
    echo "INSTALL_LOCAL_KERNEL_DEBS=${INSTALL_LOCAL_KERNEL_DEBS} but no local kernel debs were found under ${SCRIPT_DIR}" >&2
    exit 1
  fi

  local stage_dir="/tmp/orangepic-kernel-debs"
  rm -rf "${stage_dir}"
  mkdir -p "${stage_dir}"

  local staged_debs=()
  local deb_name deb_path staged_path
  for deb_path in "${debs[@]}"; do
    deb_name="$(basename "${deb_path}")"
    staged_path="${stage_dir}/${deb_name}"
    install -m 0644 "${deb_path}" "${staged_path}"
    staged_debs+=("${staged_path}")
  done

  log "Installing local RK3588 kernel packages"
  DEBIAN_FRONTEND=noninteractive "${APT_GET}" install -y "${staged_debs[@]}"
  log "Kernel packages installed. Reboot is recommended before starting the stack."
}

create_venv() {
  log "Creating project virtual environment"
  python3 -m venv --system-site-packages "${VENV_DIR}"
  "${VENV_PY}" -m pip install --upgrade pip setuptools wheel
}

install_python_packages() {
  log "Installing project Python requirements"
  "${VENV_PY}" -m pip install -r "${PROJECT_DIR}/requirements.txt"
}

install_rknn_from_pip() {
  if [[ -z "${RKNN_PIP_PACKAGE}" ]]; then
    return 1
  fi

  local package_spec="${RKNN_PIP_PACKAGE}"
  if [[ -n "${RKNN_PIP_VERSION}" ]]; then
    package_spec="${package_spec}==${RKNN_PIP_VERSION}"
  fi

  log "Trying RKNN Lite from pip: ${package_spec}"
  if "${VENV_PY}" -m pip install "${package_spec}"; then
    return 0
  fi

  if [[ -n "${RKNN_PIP_VERSION}" ]]; then
    log "Pinned RKNN Lite install failed, retrying without version pin"
    "${VENV_PY}" -m pip install "${RKNN_PIP_PACKAGE}"
    return $?
  fi

  return 1
}

install_rknn_from_local_wheel() {
  local wheel="${RKNN_WHEEL}"
  if [[ -z "${wheel}" ]]; then
    wheel="$(detect_local_rknn_wheel)"
  fi
  if [[ -z "${wheel}" ]]; then
    return 1
  fi
  if [[ ! -f "${wheel}" ]]; then
    echo "RKNN wheel does not exist: ${wheel}" >&2
    exit 1
  fi
  log "Installing RKNN Lite wheel: ${wheel}"
  "${VENV_PY}" -m pip install "${wheel}"
}

ensure_rknn_runtime() {
  if [[ "${INSTALL_AI}" != "1" ]]; then
    log "Skipping RKNN runtime check because INSTALL_AI=${INSTALL_AI}"
    return 0
  fi

  if python_import_ok "rknnlite.api"; then
    log "RKNN Lite runtime is already available"
    return 0
  fi

  install_rknn_from_pip || true
  if python_import_ok "rknnlite.api"; then
    log "RKNN Lite runtime is ready"
    return 0
  fi

  install_rknn_from_local_wheel || true

  if python_import_ok "rknnlite.api"; then
    log "RKNN Lite runtime is ready"
    return 0
  fi

  cat >&2 <<'EOF'
RKNN Lite runtime is missing.

Do one of the following and rerun:
  1. Ensure the board can access pip and rerun the script
  2. Place a local wheel such as rknn_toolkit_lite2-*.whl under the project directory and rerun
  3. Or provide a wheel path explicitly:
     sudo RKNN_WHEEL=/home/orangepi/rknn_toolkit_lite2.whl bash scripts/deploy_new_system.sh install

If you only want keyboard/mouse passthrough without AI, rerun with:
  sudo INSTALL_AI=0 bash scripts/deploy_new_system.sh install
EOF
  exit 1
}

prepare_dirs() {
  mkdir -p "${PROJECT_DIR}/generated" "${PROJECT_DIR}/models"
  chown -R "${APP_USER}:${APP_USER}" "${VENV_DIR}" "${PROJECT_DIR}/generated" "${PROJECT_DIR}/models" 2>/dev/null || true
}

verify_commands() {
  local cmd
  for cmd in python3 bash sudo v4l2-ctl modprobe udevadm pgrep; do
    command -v "${cmd}" >/dev/null 2>&1 || {
      echo "Missing command after installation: ${cmd}" >&2
      exit 1
    }
  done
}

verify_python_modules() {
  local install_ai_flag="${INSTALL_AI}"
  "${VENV_PY}" - "${install_ai_flag}" <<'PY'
import importlib
import sys

required = ["evdev", "numpy", "cv2"]
if sys.argv[1] == "1":
    required.append("rknnlite.api")

missing = []
for name in required:
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{name}: {exc}")

if missing:
    raise SystemExit("missing Python modules:\n" + "\n".join(missing))

print("python modules ok")
PY
}

verify_models() {
  if [[ "${INSTALL_AI}" != "1" ]]; then
    return 0
  fi

  local has_model=0
  compgen -G "${PROJECT_DIR}/*.rknn" >/dev/null 2>&1 && has_model=1
  if [[ "${has_model}" != "1" ]]; then
    echo "No .rknn model file found under ${PROJECT_DIR}" >&2
    exit 1
  fi
}

install_autostart_if_needed() {
  if [[ "${ENABLE_AUTOSTART}" != "1" ]]; then
    return 0
  fi
  log "Installing boot autostart service"
  APP_USER="${APP_USER}" APP_HOME="${APP_HOME}" bash "${SCRIPT_DIR}/install_start_all_autostart.sh" install
}

enable_hdmirx_overlay_if_possible() {
  if [[ ! -f "${ENABLE_HDMIRX_OVERLAY_SCRIPT}" ]]; then
    log "HDMI RX overlay helper not found, skip enabling overlay"
    return 0
  fi
  log "Ensuring HDMI RX DT overlay is enabled"
  bash "${ENABLE_HDMIRX_OVERLAY_SCRIPT}" enable
}

install_board_rknn_runtime_if_present() {
  if [[ ! -f "${INSTALL_RKNN_RUNTIME_SCRIPT}" ]]; then
    log "RKNN runtime installer helper not found, skip board runtime install"
    return 0
  fi
  if [[ ! -f "${SCRIPT_DIR}/librknnrt.so" ]]; then
    log "No bundled board-side librknnrt.so found, skip board runtime install"
    return 0
  fi
  log "Installing board-side RKNN runtime files"
  bash "${INSTALL_RKNN_RUNTIME_SCRIPT}" install
}

start_stack_if_needed() {
  if [[ "${START_AFTER_INSTALL}" != "1" ]]; then
    return 0
  fi
  log "Starting stack immediately"
  APP_USER="${APP_USER}" APP_HOME="${APP_HOME}" bash "${SCRIPT_DIR}/start_all.sh" restart
}

print_summary() {
  cat <<EOF
Deployment finished.
Project: ${PROJECT_DIR}
App user: ${APP_USER}
App home: ${APP_HOME}
Venv python: ${VENV_PY}
Autostart: ${ENABLE_AUTOSTART}
Start after install: ${START_AFTER_INSTALL}
Install AI: ${INSTALL_AI}
Local kernel debs: ${INSTALL_LOCAL_KERNEL_DEBS}

Useful commands:
  sudo bash scripts/deploy_new_system.sh verify
  sudo bash scripts/start_all.sh start
  sudo bash scripts/start_all.sh status
  sudo bash scripts/km_passthrough.sh status
  sudo reboot
EOF
}

verify_only() {
  validate_requested_mode
  require_user_exists
  [[ -x "${VENV_PY}" ]] || {
    echo "Virtual environment is missing: ${VENV_PY}" >&2
    exit 1
  }
  verify_commands
  verify_python_modules
  verify_models
  log "Verification passed"
}

install_all() {
  need_root
  validate_requested_mode
  require_user_exists
  install_apt_packages
  install_local_kernel_debs_if_needed
  create_venv
  install_python_packages
  install_board_rknn_runtime_if_present
  ensure_rknn_runtime
  prepare_dirs
  enable_hdmirx_overlay_if_possible
  verify_only
  install_autostart_if_needed
  start_stack_if_needed
  print_summary
}

case "${ACTION}" in
  install)
    install_all
    ;;
  verify)
    need_root
    verify_only
    ;;
  *)
    usage
    exit 1
    ;;
esac
