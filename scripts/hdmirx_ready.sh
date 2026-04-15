#!/usr/bin/env bash
set -euo pipefail

HDMIRX_DEVICE_ENV="${HDMIRX_DEVICE:-}"
HDMIRX_SYSFS="${HDMIRX_SYSFS:-/sys/class/hdmirx/hdmirx}"
ORANGEPI_ENV_FILE="${ORANGEPI_ENV_FILE:-/boot/orangepiEnv.txt}"
WAIT_TIMEOUT_SECONDS="${HDMIRX_WAIT_READY_TIMEOUT_SECONDS:-20}"
WAIT_INTERVAL_SECONDS="${HDMIRX_WAIT_READY_INTERVAL_SECONDS:-1}"
STREAM_TEST_ENABLED="${HDMIRX_STREAM_TEST_ENABLED:-1}"
STREAM_TEST_TIMEOUT_SECONDS="${HDMIRX_STREAM_TEST_TIMEOUT_SECONDS:-6}"
STREAM_TEST_COUNT="${HDMIRX_STREAM_TEST_COUNT:-1}"
STREAM_TEST_SKIP="${HDMIRX_STREAM_TEST_SKIP:-0}"
ACTION="${1:-status}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/hdmirx_ready.sh status
  bash scripts/hdmirx_ready.sh check
  bash scripts/hdmirx_ready.sh wait-ready

Optional env:
  HDMIRX_DEVICE=/dev/video0
  HDMIRX_WAIT_READY_TIMEOUT_SECONDS=20
  HDMIRX_WAIT_READY_INTERVAL_SECONDS=1
  HDMIRX_STREAM_TEST_ENABLED=1
  HDMIRX_STREAM_TEST_TIMEOUT_SECONDS=6
  HDMIRX_STREAM_TEST_COUNT=1
  HDMIRX_STREAM_TEST_SKIP=0
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing command: $1" >&2
    exit 1
  }
}

list_video_devices() {
  v4l2-ctl --list-devices 2>/dev/null | sed -n 's/^[[:space:]]*\(\/dev\/video[0-9]\+\)[[:space:]]*$/\1/p'
}

score_device() {
  local device="$1"
  local score=0
  local all_output
  all_output="$(v4l2-ctl -d "${device}" --all 2>&1 || true)"
  local lower
  lower="$(printf '%s' "${all_output}" | tr '[:upper:]' '[:lower:]')"
  [[ "${lower}" == *hdmi* ]] && score=$((score + 5))
  [[ "${lower}" == *rx* || "${lower}" == *hdmirx* ]] && score=$((score + 5))
  [[ "${lower}" == *"dv timings"* ]] && score=$((score + 3))
  if v4l2-ctl -d "${device}" --list-formats-ext >/dev/null 2>&1; then
    score=$((score + 1))
  fi
  echo "${score}"
}

auto_detect_device() {
  local best_device=""
  local best_score=-1
  local device score
  while IFS= read -r device; do
    [[ -n "${device}" ]] || continue
    score="$(score_device "${device}")"
    if (( score > best_score )); then
      best_score="${score}"
      best_device="${device}"
    fi
  done < <(list_video_devices)

  if [[ -n "${best_device}" ]]; then
    echo "${best_device}"
    return 0
  fi
  return 1
}

resolve_device() {
  if [[ -n "${HDMIRX_DEVICE_ENV}" ]]; then
    [[ -e "${HDMIRX_DEVICE_ENV}" ]] || {
      echo "Configured HDMI RX device does not exist: ${HDMIRX_DEVICE_ENV}" >&2
      return 1
    }
    echo "${HDMIRX_DEVICE_ENV}"
    return 0
  fi
  auto_detect_device
}

read_hdmirx_sysfs() {
  local path="$1"
  [[ -e "${path}" ]] || return 0
  echo "[sysfs] ${path}"
  cat "${path}" 2>/dev/null || true
}

dv_timings_text() {
  local device="$1"
  v4l2-ctl -d "${device}" --get-dv-timings 2>&1 || true
}

timings_ready() {
  local text="$1"
  local lower
  lower="$(printf '%s' "${text}" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "${lower}" ]]; then
    return 1
  fi
  [[ "${lower}" == *"no signal"* ]] && return 1
  [[ "${lower}" == *"no lock"* ]] && return 1
  [[ "${lower}" == *"not detected"* ]] && return 1
  [[ "${lower}" == *"failed"* ]] && return 1
  [[ "${lower}" == *"cannot"* ]] && return 1
  [[ "${lower}" == *"active width"* ]] && return 0
  [[ "${lower}" == *"pixelclock"* ]] && return 0
  [[ "${lower}" == *"bt timings"* ]] && return 0
  return 1
}

stream_test() {
  local device="$1"
  if [[ "${STREAM_TEST_ENABLED}" != "1" ]]; then
    return 0
  fi

  local args=(
    -d "${device}"
    --stream-mmap=4
    --stream-count="${STREAM_TEST_COUNT}"
    --stream-to=/dev/null
  )
  if [[ "${STREAM_TEST_SKIP}" != "0" ]]; then
    args+=("--stream-skip=${STREAM_TEST_SKIP}")
  fi

  if command -v timeout >/dev/null 2>&1; then
    timeout "${STREAM_TEST_TIMEOUT_SECONDS}s" v4l2-ctl "${args[@]}" >/tmp/hdmirx_stream_test.log 2>&1 || return 1
    return 0
  fi

  v4l2-ctl "${args[@]}" >/tmp/hdmirx_stream_test.log 2>&1
}

print_status() {
  local device="${1:-}"
  echo "HDMI RX sysfs: ${HDMIRX_SYSFS}"
  if [[ -d "${HDMIRX_SYSFS}" ]]; then
    read_hdmirx_sysfs "${HDMIRX_SYSFS}/edid"
  else
    echo "HDMI RX sysfs directory not found."
  fi
  echo
  if [[ -z "${device}" ]]; then
    echo "HDMI RX device: not found"
    if [[ -f "${ORANGEPI_ENV_FILE}" ]]; then
      echo
      echo "[orangepiEnv overlays]"
      grep -n '^overlays=' "${ORANGEPI_ENV_FILE}" || echo "No overlays= entry in ${ORANGEPI_ENV_FILE}"
      if ! grep -Eq '^overlays=.*(^|[[:space:]])hdmirx([[:space:]]|$)' "${ORANGEPI_ENV_FILE}"; then
        echo "Hint: HDMI RX overlay is probably not enabled. Run: sudo bash scripts/enable_hdmirx_overlay.sh enable"
      fi
    fi
    return 0
  fi
  echo "HDMI RX device: ${device}"
  echo
  echo "[v4l2-ctl --all]"
  v4l2-ctl -d "${device}" --all 2>&1 || true
  echo
  echo "[v4l2-ctl --get-dv-timings]"
  dv_timings_text "${device}"
}

check_ready_once() {
  local device="$1"
  [[ -e "${device}" ]] || {
    echo "HDMI RX device missing: ${device}" >&2
    return 1
  }

  if ! v4l2-ctl -d "${device}" --all >/dev/null 2>&1; then
    echo "Cannot query HDMI RX device: ${device}" >&2
    return 1
  fi

  local timings
  timings="$(dv_timings_text "${device}")"
  if ! timings_ready "${timings}"; then
    echo "HDMI RX signal is not ready on ${device}" >&2
    echo "${timings}" >&2
    return 1
  fi

  if ! stream_test "${device}"; then
    echo "HDMI RX stream test failed on ${device}" >&2
    if [[ -f /tmp/hdmirx_stream_test.log ]]; then
      tail -n 40 /tmp/hdmirx_stream_test.log >&2 || true
    fi
    return 1
  fi

  echo "HDMI RX ready: ${device}"
  echo "${timings}"
  return 0
}

wait_ready() {
  local device="$1"
  local timeout_seconds interval_seconds start_ts now_ts
  timeout_seconds="${WAIT_TIMEOUT_SECONDS}"
  interval_seconds="${WAIT_INTERVAL_SECONDS}"
  start_ts="$(date +%s)"

  while true; do
    if check_ready_once "${device}"; then
      return 0
    fi
    now_ts="$(date +%s)"
    if (( now_ts - start_ts >= timeout_seconds )); then
      echo "Timed out waiting for HDMI RX readiness after ${timeout_seconds}s: ${device}" >&2
      echo "Hint: after changing EDID, replug the HDMI cable or force the host to redetect the display output." >&2
      return 1
    fi
    sleep "${interval_seconds}"
  done
}

main() {
  need_cmd v4l2-ctl

  local device=""
  device="$(resolve_device || true)"

  case "${ACTION}" in
    status)
      print_status "${device}"
      ;;
    check)
      [[ -n "${device}" ]] || {
        echo "No /dev/video* device found for HDMI RX." >&2
        exit 1
      }
      check_ready_once "${device}"
      ;;
    wait-ready)
      [[ -n "${device}" ]] || {
        echo "No /dev/video* device found for HDMI RX." >&2
        exit 1
      }
      wait_ready "${device}"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
