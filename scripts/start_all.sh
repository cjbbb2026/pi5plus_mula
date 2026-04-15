#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="${PROJECT_DIR}/app"
GENERATED_DIR="${PROJECT_DIR}/generated"
PRECISION_DEBUG_DIR="${GENERATED_DIR}/aim_precision_debug"
KM_SCRIPT="${SCRIPT_DIR}/km_passthrough.sh"
PERF_SCRIPT="${SCRIPT_DIR}/perf_mode.sh"
HDMIRX_EDID_SCRIPT="${SCRIPT_DIR}/hdmirx_edid.sh"
HDMIRX_READY_SCRIPT="${SCRIPT_DIR}/hdmirx_ready.sh"
RKNN_CHECK_SCRIPT="${SCRIPT_DIR}/check_rknn_runtime.sh"
AI_PID_FILE="${PROJECT_DIR}/.ai.pid"
WEB_PID_FILE="${PROJECT_DIR}/.web.pid"
AI_CMD_FILE="${PROJECT_DIR}/.ai_command.json"
CONFIG_DIR="${PROJECT_DIR}/config"
LAST_MODEL_FILE="${PROJECT_DIR}/.ai_last_model.txt"
STATE_FILE="${PROJECT_DIR}/.ai_state.json"
PASSTHROUGH_LOG="${PROJECT_DIR}/.passthrough.log"
AI_LOG_FILE="${PROJECT_DIR}/.ai.log"
WEB_LOG_FILE="${PROJECT_DIR}/.web.log"
GUI_ENV_FILE="${PROJECT_DIR}/.gui_env"
ACTION="${1:-start}"

cd "${PROJECT_DIR}"

ORIGINAL_USER="${ORIGINAL_USER:-${SUDO_USER:-${USER:-orangepi}}}"
ORIGINAL_HOME="${ORIGINAL_HOME:-$(getent passwd "${ORIGINAL_USER}" | cut -d: -f6 || true)}"
if [[ -z "${ORIGINAL_HOME}" ]]; then
  ORIGINAL_HOME="/home/${ORIGINAL_USER}"
fi

GUI_DISPLAY="${DISPLAY:-}"
GUI_XAUTHORITY="${XAUTHORITY:-}"
GUI_WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}"
GUI_XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}"
GUI_DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}"

detect_python_bin() {
  local candidates=()
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates+=("${PYTHON_BIN}")
  fi
  if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
    candidates+=("${PROJECT_DIR}/.venv/bin/python")
  fi
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("python3")
  fi
  if command -v python >/dev/null 2>&1; then
    candidates+=("python")
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ "${candidate}" == */* ]]; then
      [[ -x "${candidate}" ]] && { echo "${candidate}"; return 0; }
      continue
    fi
    if command -v "${candidate}" >/dev/null 2>&1; then
      echo "${candidate}"
      return 0
    fi
  done

  echo "python3 not found, and project venv is missing: ${PROJECT_DIR}/.venv/bin/python" >&2
  exit 1
}

write_gui_env_file() {
  cat > "${GUI_ENV_FILE}" <<EOF
DISPLAY=${GUI_DISPLAY}
XAUTHORITY=${GUI_XAUTHORITY}
WAYLAND_DISPLAY=${GUI_WAYLAND_DISPLAY}
XDG_RUNTIME_DIR=${GUI_XDG_RUNTIME_DIR}
DBUS_SESSION_BUS_ADDRESS=${GUI_DBUS_SESSION_BUS_ADDRESS}
EOF
}

load_gui_env_file() {
  [[ -f "${GUI_ENV_FILE}" ]] || return 0
  while IFS='=' read -r key value; do
    case "${key}" in
      DISPLAY) [[ -z "${GUI_DISPLAY}" ]] && GUI_DISPLAY="${value}" ;;
      XAUTHORITY) [[ -z "${GUI_XAUTHORITY}" ]] && GUI_XAUTHORITY="${value}" ;;
      WAYLAND_DISPLAY) [[ -z "${GUI_WAYLAND_DISPLAY}" ]] && GUI_WAYLAND_DISPLAY="${value}" ;;
      XDG_RUNTIME_DIR) [[ -z "${GUI_XDG_RUNTIME_DIR}" ]] && GUI_XDG_RUNTIME_DIR="${value}" ;;
      DBUS_SESSION_BUS_ADDRESS) [[ -z "${GUI_DBUS_SESSION_BUS_ADDRESS}" ]] && GUI_DBUS_SESSION_BUS_ADDRESS="${value}" ;;
    esac
  done < "${GUI_ENV_FILE}"
  return 0
}

if [[ ${EUID} -ne 0 ]]; then
  write_gui_env_file
  exec sudo DISPLAY="${GUI_DISPLAY}" XAUTHORITY="${GUI_XAUTHORITY}" WAYLAND_DISPLAY="${GUI_WAYLAND_DISPLAY}" XDG_RUNTIME_DIR="${GUI_XDG_RUNTIME_DIR}" DBUS_SESSION_BUS_ADDRESS="${GUI_DBUS_SESSION_BUS_ADDRESS}" bash "$0" "$@"
fi

load_gui_env_file

PYTHON_BIN="$(detect_python_bin)"
MODEL_STORE_FILE="${MODEL_STORE_FILE:-${PROJECT_DIR}/.ai_models.json}"
MODEL_DIR="${MODEL_DIR:-${PROJECT_DIR}/models}"
CONFIG_DIR="${CONFIG_DIR:-${PROJECT_DIR}/config}"
LAST_MODEL_FILE="${LAST_MODEL_FILE:-${PROJECT_DIR}/.ai_last_model.txt}"
DEFAULT_AI_MODEL="$("${PYTHON_BIN}" - "${PROJECT_DIR}" "${MODEL_STORE_FILE}" "${MODEL_DIR}" "${CONFIG_DIR}" "${LAST_MODEL_FILE}" "${PROJECT_DIR}/yolo261n-rk3588.rknn" <<'PY'
import sys
from pathlib import Path
project_dir = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(project_dir / 'app'))
from model_manager import ModelStore
store = ModelStore(Path(sys.argv[2]), Path(sys.argv[3]), project_dir, Path(sys.argv[4]), Path(sys.argv[5]))
fallback = Path(sys.argv[6]).resolve()
selected = store.selected_model_path()
print(selected if selected is not None else fallback)
PY
)"
AI_MODEL="${AI_MODEL:-${DEFAULT_AI_MODEL}}"
DEFAULT_RUNTIME_CONFIG_FILE="$("${PYTHON_BIN}" - "${PROJECT_DIR}" "${MODEL_STORE_FILE}" "${MODEL_DIR}" "${CONFIG_DIR}" "${LAST_MODEL_FILE}" "${CONFIG_DIR}/default.json" <<'PY'
import sys
from pathlib import Path
project_dir = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(project_dir / 'app'))
from model_manager import ModelStore
store = ModelStore(Path(sys.argv[2]), Path(sys.argv[3]), project_dir, Path(sys.argv[4]), Path(sys.argv[5]))
fallback = Path(sys.argv[6]).resolve()
selected = store.selected_model_config_path()
print(selected if selected is not None else fallback)
PY
)"
RUNTIME_CONFIG_FILE="${RUNTIME_CONFIG_FILE:-${DEFAULT_RUNTIME_CONFIG_FILE}}"
AI_DEVICE="${AI_DEVICE:-auto}"
AI_BACKEND="${AI_BACKEND:-v4l2-raw}"
AI_CROP_WIDTH="${AI_CROP_WIDTH:-500}"
AI_CROP_HEIGHT="${AI_CROP_HEIGHT:-500}"
AI_PROCESS_WIDTH="${AI_PROCESS_WIDTH:-0}"
AI_PROCESS_HEIGHT="${AI_PROCESS_HEIGHT:-0}"
AI_SHOW="${AI_SHOW:-0}"
AI_EXTRA_ARGS="${AI_EXTRA_ARGS:---print-every 2}"
WEB_ENABLE="${WEB_ENABLE:-1}"
WEB_HOST="${WEB_HOST:-0.0.0.0}"
WEB_PORT="${WEB_PORT:-8080}"
APPLY_PERF_MODE="${APPLY_PERF_MODE:-1}"
APPLY_HDMIRX_EDID="${APPLY_HDMIRX_EDID:-1}"
HDMIRX_EDID_ACTION="${HDMIRX_EDID_ACTION:-}"
HDMIRX_EDID_FALLBACK_ACTION="${HDMIRX_EDID_FALLBACK_ACTION:-standard-dual}"
REQUIRE_HDMIRX_READY="${REQUIRE_HDMIRX_READY:-1}"
HDMIRX_EDID_SETTLE_SECONDS="${HDMIRX_EDID_SETTLE_SECONDS:-5}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/start_all.sh start
  ./scripts/start_all.sh stop
  ./scripts/start_all.sh restart
  ./scripts/start_all.sh status

Optional env:
  AI_MODEL=/home/orangepi/Desktop/core/yolo261n-rk3588.rknn
  MODEL_STORE_FILE=/home/orangepi/Desktop/core/.ai_models.json
  CONFIG_DIR=/home/orangepi/Desktop/core/config
  LAST_MODEL_FILE=/home/orangepi/Desktop/core/.ai_last_model.txt
  MODEL_DIR=/home/orangepi/Desktop/core/models
  AI_DEVICE=auto
  AI_BACKEND=v4l2-raw
  AI_CROP_WIDTH=500
  AI_CROP_HEIGHT=500
  AI_PROCESS_WIDTH=0
  AI_PROCESS_HEIGHT=0
  AI_SHOW=0
  AI_EXTRA_ARGS="--print-every 2"
  WEB_ENABLE=1
  WEB_HOST=0.0.0.0
  WEB_PORT=8080
  APPLY_PERF_MODE=1
  APPLY_HDMIRX_EDID=1
  HDMIRX_EDID_ACTION=
  HDMIRX_EDID_FALLBACK_ACTION=standard-dual
  REQUIRE_HDMIRX_READY=1
  HDMIRX_EDID_SETTLE_SECONDS=5
EOF
}

resolve_hdmirx_edid_action() {
  if [[ -n "${HDMIRX_EDID_ACTION}" ]]; then
    echo "${HDMIRX_EDID_ACTION}"
    return 0
  fi
  if [[ -f "${APP_DIR}/hdmirx_edid.py" ]]; then
    local saved_action
    saved_action="$("${PYTHON_BIN}" "${APP_DIR}/hdmirx_edid.py" current-profile 2>/dev/null || true)"
    saved_action="$(printf '%s' "${saved_action}" | tr -d '\r' | tail -n 1 | xargs)"
    if [[ -n "${saved_action}" ]]; then
      echo "${saved_action}"
      return 0
    fi
  fi
  echo "${HDMIRX_EDID_FALLBACK_ACTION}"
}

ensure_log_files() {
  : > "${PASSTHROUGH_LOG}"
  : > "${AI_LOG_FILE}"
  : > "${WEB_LOG_FILE}"
  chown "${ORIGINAL_USER}:${ORIGINAL_USER}" "${AI_LOG_FILE}" "${WEB_LOG_FILE}" 2>/dev/null || true
}

ensure_runtime_dirs() {
  mkdir -p "${GENERATED_DIR}" "${PRECISION_DEBUG_DIR}" "${CONFIG_DIR}"
  chown "${ORIGINAL_USER}:${ORIGINAL_USER}" "${GENERATED_DIR}" "${PRECISION_DEBUG_DIR}" "${CONFIG_DIR}" 2>/dev/null || true
  chmod u+rwx "${GENERATED_DIR}" "${PRECISION_DEBUG_DIR}" "${CONFIG_DIR}" 2>/dev/null || true
}

passthrough_ctl() {
  bash "${KM_SCRIPT}" "$@"
}

apply_perf_mode_if_needed() {
  if [[ "${APPLY_PERF_MODE}" != "1" ]]; then
    echo "Skip performance mode: APPLY_PERF_MODE=${APPLY_PERF_MODE}"
    return 0
  fi
  if [[ ! -f "${PERF_SCRIPT}" ]]; then
    echo "Warning: perf script not found: ${PERF_SCRIPT}" >&2
    return 0
  fi
  echo "Enabling performance mode..."
  if ! bash "${PERF_SCRIPT}" on; then
    echo "Warning: failed to enable performance mode." >&2
  fi
}

apply_hdmirx_edid_if_needed() {
  local resolved_action
  if [[ "${APPLY_HDMIRX_EDID}" != "1" ]]; then
    echo "Skip HDMI RX EDID setup: APPLY_HDMIRX_EDID=${APPLY_HDMIRX_EDID}"
    return 0
  fi
  if [[ ! -f "${HDMIRX_EDID_SCRIPT}" ]]; then
    echo "Warning: HDMI RX EDID script not found: ${HDMIRX_EDID_SCRIPT}" >&2
    return 0
  fi
  resolved_action="$(resolve_hdmirx_edid_action)"
  if [[ -z "${resolved_action}" ]]; then
    echo "Warning: HDMIRX_EDID_ACTION is empty, skip HDMI RX EDID setup." >&2
    return 0
  fi
  echo "Applying HDMI RX EDID preset: ${resolved_action}"
  if command -v timeout >/dev/null 2>&1; then
    if ! timeout 12s bash "${HDMIRX_EDID_SCRIPT}" "${resolved_action}"; then
      echo "Warning: failed to apply HDMI RX EDID preset: ${resolved_action}" >&2
    fi
    if [[ "${HDMIRX_EDID_SETTLE_SECONDS}" != "0" ]]; then
      echo "Waiting ${HDMIRX_EDID_SETTLE_SECONDS}s for the HDMI source to re-read EDID..."
      sleep "${HDMIRX_EDID_SETTLE_SECONDS}"
    fi
    return 0
  fi
  if ! bash "${HDMIRX_EDID_SCRIPT}" "${resolved_action}"; then
    echo "Warning: failed to apply HDMI RX EDID preset: ${resolved_action}" >&2
  fi
  if [[ "${HDMIRX_EDID_SETTLE_SECONDS}" != "0" ]]; then
    echo "Waiting ${HDMIRX_EDID_SETTLE_SECONDS}s for the HDMI source to re-read EDID..."
    sleep "${HDMIRX_EDID_SETTLE_SECONDS}"
  fi
}

ensure_hdmirx_ready_if_needed() {
  if [[ "${REQUIRE_HDMIRX_READY}" != "1" ]]; then
    echo "Skip HDMI RX readiness check: REQUIRE_HDMIRX_READY=${REQUIRE_HDMIRX_READY}"
    return 0
  fi
  if [[ ! -f "${HDMIRX_READY_SCRIPT}" ]]; then
    echo "Warning: HDMI RX readiness script not found: ${HDMIRX_READY_SCRIPT}" >&2
    return 0
  fi
  echo "Waiting for HDMI RX input to become ready..."
  bash "${HDMIRX_READY_SCRIPT}" wait-ready
}

ensure_rknn_runtime_compatible() {
  if [[ ! -f "${RKNN_CHECK_SCRIPT}" ]]; then
    echo "Warning: RKNN compatibility check script not found: ${RKNN_CHECK_SCRIPT}" >&2
    return 0
  fi
  if [[ ! -f "${AI_MODEL}" ]]; then
    echo "Warning: AI model file not found, skip RKNN compatibility check: ${AI_MODEL}" >&2
    return 0
  fi
  echo "Checking RKNN runtime compatibility..."
  PYTHON_BIN="${PYTHON_BIN}" bash "${RKNN_CHECK_SCRIPT}" "${AI_MODEL}"
}

process_matches_hint() {
  local pid="$1"
  local hint="$2"
  [[ -z "${hint}" ]] && return 0
  [[ ! -r "/proc/${pid}/cmdline" ]] && return 1
  tr '\0' ' ' < "/proc/${pid}/cmdline" | grep -F -- "${hint}" >/dev/null 2>&1
}

pid_is_running() {
  local pid_file="$1"
  local hint="${2:-}"
  if [[ ! -f "${pid_file}" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "${pid_file}" 2>/dev/null || true)"
  [[ -z "${pid}" ]] && return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  process_matches_hint "${pid}" "${hint}"
}

find_matching_pids() {
  local hint="$1"
  [[ -z "${hint}" ]] && return 0
  pgrep -u "${ORIGINAL_USER}" -f -- "${hint}" 2>/dev/null || true
}

kill_pid_gracefully() {
  local pid="$1"
  kill "${pid}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  kill -9 "${pid}" 2>/dev/null || true
}

kill_matching_processes() {
  local hint="$1"
  local pid
  local pids
  pids="$(find_matching_pids "${hint}")"
  [[ -z "${pids}" ]] && return 0
  for pid in ${pids}; do
    kill_pid_gracefully "${pid}"
  done
}

ai_is_running() {
  pid_is_running "${AI_PID_FILE}" "app/ai.py"
}

web_is_running() {
  pid_is_running "${WEB_PID_FILE}" "app/web_console.py"
}

stop_by_pidfile() {
  local pid_file="$1"
  local hint="${2:-}"
  if pid_is_running "${pid_file}" "${hint}"; then
    local pid
    pid="$(cat "${pid_file}")"
    kill_pid_gracefully "${pid}"
  fi
  if [[ -n "${hint}" ]]; then
    kill_matching_processes "${hint}"
  fi
  rm -f "${pid_file}"
}

run_as_user_bg() {
  local pid_file="$1"
  local log_file="$2"
  shift 2

  local cmd_quoted=()
  local arg
  for arg in "$@"; do
    cmd_quoted+=("$(printf '%q' "${arg}")")
  done
  local cmd_string="${cmd_quoted[*]}"

  local xauth="${GUI_XAUTHORITY}"
  if [[ -z "${xauth}" && -f "${ORIGINAL_HOME}/.Xauthority" ]]; then
    xauth="${ORIGINAL_HOME}/.Xauthority"
  fi

  local env_cmd="export HOME=$(printf '%q' "${ORIGINAL_HOME}");"
  if [[ -n "${GUI_DISPLAY}" ]]; then
    env_cmd+=" export DISPLAY=$(printf '%q' "${GUI_DISPLAY}");"
  fi
  if [[ -n "${xauth}" ]]; then
    env_cmd+=" export XAUTHORITY=$(printf '%q' "${xauth}");"
  fi
  if [[ -n "${GUI_WAYLAND_DISPLAY}" ]]; then
    env_cmd+=" export WAYLAND_DISPLAY=$(printf '%q' "${GUI_WAYLAND_DISPLAY}");"
  fi
  if [[ -n "${GUI_XDG_RUNTIME_DIR}" ]]; then
    env_cmd+=" export XDG_RUNTIME_DIR=$(printf '%q' "${GUI_XDG_RUNTIME_DIR}");"
  fi
  if [[ -n "${GUI_DBUS_SESSION_BUS_ADDRESS}" ]]; then
    env_cmd+=" export DBUS_SESSION_BUS_ADDRESS=$(printf '%q' "${GUI_DBUS_SESSION_BUS_ADDRESS}");"
  fi

  runuser -u "${ORIGINAL_USER}" -- bash -lc "${env_cmd} cd $(printf '%q' "${PROJECT_DIR}"); nohup ${cmd_string} >> $(printf '%q' "${log_file}") 2>&1 & echo \$! > $(printf '%q' "${pid_file}")"
}

write_ai_command_file() {
  "${PYTHON_BIN}" - "${AI_CMD_FILE}" "${PROJECT_DIR}" "${ORIGINAL_HOME}" "${AI_LOG_FILE}" "${AI_PID_FILE}" "$@" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
payload = {
    'project_dir': sys.argv[2],
    'home': sys.argv[3],
    'log_file': sys.argv[4],
    'pid_file': sys.argv[5],
    'cmd': sys.argv[6:],
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
PY
  chown "${ORIGINAL_USER}:${ORIGINAL_USER}" "${AI_CMD_FILE}" 2>/dev/null || true
}

sync_selected_model_defaults() {
  "${PYTHON_BIN}" - "${PROJECT_DIR}" "${MODEL_STORE_FILE}" "${MODEL_DIR}" "${CONFIG_DIR}" "${LAST_MODEL_FILE}" <<'PY'
import sys
from pathlib import Path
project_dir = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(project_dir / 'app'))
from model_manager import ModelStore
store = ModelStore(Path(sys.argv[2]), Path(sys.argv[3]), project_dir, Path(sys.argv[4]), Path(sys.argv[5]))
config_path = store.selected_model_config_path()
if config_path is not None:
    print(config_path)
PY
  chown "${ORIGINAL_USER}:${ORIGINAL_USER}" "${CONFIG_DIR}" "${LAST_MODEL_FILE}" 2>/dev/null || true
  find "${CONFIG_DIR}" -maxdepth 1 -type f -name '*.json' -exec chown "${ORIGINAL_USER}:${ORIGINAL_USER}" {} + 2>/dev/null || true
}

start_ai() {
  ensure_log_files
  ensure_runtime_dirs
  stop_by_pidfile "${AI_PID_FILE}" "app/ai.py"
  sync_selected_model_defaults
  local current_ai_model="${AI_MODEL}"
  local current_runtime_config="${RUNTIME_CONFIG_FILE}"

  echo "Starting AI with backend: ${AI_BACKEND}"

  local ai_cmd=(
    "${PYTHON_BIN}" "${APP_DIR}/ai.py"
    --model "${current_ai_model}"
    --device "${AI_DEVICE}"
    --backend "${AI_BACKEND}"
    --crop-width "${AI_CROP_WIDTH}"
    --crop-height "${AI_CROP_HEIGHT}"
    --process-width "${AI_PROCESS_WIDTH}"
    --process-height "${AI_PROCESS_HEIGHT}"
    --runtime-config-file "${current_runtime_config}"
    --state-file "${STATE_FILE}"
  )

  if [[ "${AI_SHOW}" == "1" ]]; then
    ai_cmd+=(--show)
  fi

  if [[ -n "${AI_EXTRA_ARGS}" ]]; then
    # shellcheck disable=SC2206
    local extra_args=( ${AI_EXTRA_ARGS} )
    ai_cmd+=("${extra_args[@]}")
  fi

  write_ai_command_file "${ai_cmd[@]}"
  run_as_user_bg "${AI_PID_FILE}" "${AI_LOG_FILE}" "${ai_cmd[@]}"
}

start_web() {
  ensure_log_files
  ensure_runtime_dirs
  stop_by_pidfile "${WEB_PID_FILE}" "app/web_console.py"

  if [[ "${WEB_ENABLE}" != "1" ]]; then
    rm -f "${WEB_PID_FILE}"
    return 0
  fi

  local web_cmd=(
    "${PYTHON_BIN}" "${APP_DIR}/web_console.py"
    --host "${WEB_HOST}"
    --port "${WEB_PORT}"
    --state-file "${PROJECT_DIR}/.ai_state.json"
    --ai-log "${AI_LOG_FILE}"
    --passthrough-log "${PASSTHROUGH_LOG}"
    --ai-pid-file "${AI_PID_FILE}"
    --passthrough-socket "/tmp/km_passthrough.sock"
    --ai-cmd-file "${AI_CMD_FILE}"
    --km-script "${KM_SCRIPT}"
    --model-store-file "${MODEL_STORE_FILE}"
    --model-dir "${MODEL_DIR}"
    --config-dir "${CONFIG_DIR}"
    --last-model-file "${LAST_MODEL_FILE}"
  )

  run_as_user_bg "${WEB_PID_FILE}" "${WEB_LOG_FILE}" "${web_cmd[@]}"

  sleep 1
  if ! web_is_running; then
    echo "Web start failed. Recent log:" >&2
    tail -n 40 "${WEB_LOG_FILE}" >&2 || true
    return 1
  fi
}

status_all() {
  echo "Passthrough socket: /tmp/km_passthrough.sock"
  echo "Passthrough log: ${PASSTHROUGH_LOG}"
  echo "AI log: ${AI_LOG_FILE}"
  echo "Web log: ${WEB_LOG_FILE}"
  echo "AI user: ${ORIGINAL_USER}"

  if ai_is_running; then
    echo "AI: running, PID=$(cat "${AI_PID_FILE}")"
  else
    echo "AI: stopped"
  fi

  if web_is_running; then
    echo "Web: running, PID=$(cat "${WEB_PID_FILE}") URL=http://${WEB_HOST}:${WEB_PORT}"
  else
    echo "Web: stopped"
  fi

  echo "Passthrough status:"
  passthrough_ctl status || true
  echo "HDMI RX status:"
  if [[ -f "${HDMIRX_READY_SCRIPT}" ]]; then
    bash "${HDMIRX_READY_SCRIPT}" status || true
  else
    echo "HDMI RX readiness script not found: ${HDMIRX_READY_SCRIPT}"
  fi
}

start_all() {
  ensure_runtime_dirs
  apply_perf_mode_if_needed
  apply_hdmirx_edid_if_needed
  ensure_hdmirx_ready_if_needed
  ensure_rknn_runtime_compatible
  passthrough_ctl stop || true
  passthrough_ctl start-auto-mouse
  start_ai
  start_web
  sleep 1
  status_all
}

stop_all() {
  stop_by_pidfile "${WEB_PID_FILE}" "app/web_console.py"
  stop_by_pidfile "${AI_PID_FILE}" "app/ai.py"
  passthrough_ctl stop || true
  status_all
}

case "${ACTION}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  restart)
    stop_all
    start_all
    ;;
  status)
    status_all
    ;;
  *)
    usage
    exit 1
    ;;
esac
