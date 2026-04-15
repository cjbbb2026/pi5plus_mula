#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_PY="${PROJECT_DIR}/app/passthrough.py"

GADGET_DIR="/sys/kernel/config/usb_gadget/opi_hid_passthrough"
PID_FILE="${PROJECT_DIR}/.passthrough.pid"
LOG_FILE="${PROJECT_DIR}/.passthrough.log"
SCAN_INTERVAL="${SCAN_INTERVAL:-5.0}"
MODE_NODE_DEFAULT="/sys/kernel/debug/usb/fc000000.usb/mode"
ACTION="${1:-start}"
KEYBOARD_OUT=""
MOUSE_OUT=""
USB_VID="${USB_VID:-0x1d6b}"
USB_PID="${USB_PID:-0x0104}"
USB_BCD_USB="${USB_BCD_USB:-0x0200}"
USB_BCD_DEVICE="${USB_BCD_DEVICE:-0x0100}"
USB_MANUFACTURER="${USB_MANUFACTURER:-OrangePi}"
USB_PRODUCT="${USB_PRODUCT:-OrangePi Keyboard Mouse Passthrough}"
USB_SERIAL="${USB_SERIAL:-OPI5P-KM-001}"
DETECTED_MOUSE_EVENT=""

usage() {
  cat <<'USAGE'
Usage:
  sudo bash scripts/km_passthrough.sh start
  sudo bash scripts/km_passthrough.sh stop
  sudo bash scripts/km_passthrough.sh restart
  sudo bash scripts/km_passthrough.sh status
  sudo bash scripts/km_passthrough.sh list
  sudo bash scripts/km_passthrough.sh setup
  sudo bash scripts/km_passthrough.sh teardown
  sudo bash scripts/km_passthrough.sh show-mouse-id
  sudo bash scripts/km_passthrough.sh start-auto-mouse

Optional env:
  SCAN_INTERVAL=5.0
  MODE_NODE=/sys/kernel/debug/usb/fc000000.usb/mode
  USB_VID=0x046d
  USB_PID=0xc077
  USB_MANUFACTURER='Logitech'
  USB_PRODUCT='USB Optical Mouse'
  USB_SERIAL='12345678'
USAGE
}

require_root() {
  if [[ ${EUID} -ne 0 ]]; then
    echo "Please run as root."
    exit 1
  fi
}

mount_if_needed() {
  local fs_type="$1"
  local target="$2"
  if ! mountpoint -q "${target}" 2>/dev/null; then
    mkdir -p "${target}"
    mount -t "${fs_type}" "${fs_type}" "${target}"
  fi
}

choose_python() {
  if command -v python3 >/dev/null 2>&1 && python3 -c "import evdev" >/dev/null 2>&1; then
    echo "python3"
    return
  fi

  if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]] && "${PROJECT_DIR}/.venv/bin/python" -c "import evdev" >/dev/null 2>&1; then
    echo "${PROJECT_DIR}/.venv/bin/python"
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return
  fi

  echo "python3 not found." >&2
  exit 1
}

ensure_evdev() {
  if command -v python3 >/dev/null 2>&1 && python3 -c "import evdev" >/dev/null 2>&1; then
    return
  fi

  if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]] && "${PROJECT_DIR}/.venv/bin/python" -c "import evdev" >/dev/null 2>&1; then
    return
  fi

  cat >&2 <<'MSG'
Python package 'evdev' is not available.
Install one of these:
  sudo apt install -y python3-evdev
or:
  python3 -m venv .venv
  . .venv/bin/activate
  pip install -r requirements.txt
MSG
  exit 1
}

find_mode_node() {
  if [[ -n "${MODE_NODE:-}" && -e "${MODE_NODE}" ]]; then
    echo "${MODE_NODE}"
    return
  fi

  if [[ -e "${MODE_NODE_DEFAULT}" ]]; then
    echo "${MODE_NODE_DEFAULT}"
    return
  fi

  find /sys/kernel/debug/usb -maxdepth 2 -type f -name mode 2>/dev/null | head -n 1
}

read_mode() {
  local node="$1"
  tr -d '\r\n\0' < "${node}" 2>/dev/null || true
}

switch_to_device_mode() {
  mount_if_needed debugfs /sys/kernel/debug

  local mode_node
  mode_node="$(find_mode_node)"
  if [[ -z "${mode_node}" || ! -e "${mode_node}" ]]; then
    echo "USB mode node not found under /sys/kernel/debug/usb" >&2
    exit 1
  fi

  echo "Mode node: ${mode_node}"
  echo device > "${mode_node}" || true
  sleep 1

  local current
  current="$(read_mode "${mode_node}")"
  echo "USB mode: ${current}"

  if [[ "${current}" != "device" ]]; then
    echo "Failed to switch USB to device mode." >&2
    exit 1
  fi
}

load_gadget_support() {
  modprobe libcomposite 2>/dev/null || true
  modprobe usb_f_hid 2>/dev/null || modprobe g_hid 2>/dev/null || true
}

unbind_other_gadgets() {
  local udc_file
  for udc_file in /sys/kernel/config/usb_gadget/*/UDC; do
    [[ -e "${udc_file}" ]] || continue
    [[ "${udc_file}" == "${GADGET_DIR}/UDC" ]] && continue
    if [[ -s "${udc_file}" ]]; then
      echo "" > "${udc_file}" 2>/dev/null || true
    fi
  done
}

teardown_gadget_internal() {
  if [[ ! -d "${GADGET_DIR}" ]]; then
    return 0
  fi

  if [[ -e "${GADGET_DIR}/UDC" ]]; then
    echo "" > "${GADGET_DIR}/UDC" 2>/dev/null || true
  fi

  rm -f "${GADGET_DIR}/configs/c.1/hid.keyboard" 2>/dev/null || true
  rm -f "${GADGET_DIR}/configs/c.1/hid.mouse" 2>/dev/null || true
  rmdir "${GADGET_DIR}/functions/hid.keyboard" 2>/dev/null || true
  rmdir "${GADGET_DIR}/functions/hid.mouse" 2>/dev/null || true
  rmdir "${GADGET_DIR}/configs/c.1/strings/0x409" 2>/dev/null || true
  rmdir "${GADGET_DIR}/configs/c.1" 2>/dev/null || true
  rmdir "${GADGET_DIR}/strings/0x409" 2>/dev/null || true
  rmdir "${GADGET_DIR}" 2>/dev/null || true
}

resolve_hid_nodes() {
  local nodes=()
  local path
  while IFS= read -r path; do
    [[ -n "${path}" ]] && nodes+=("${path}")
  done < <(find /dev -maxdepth 1 -type c -name 'hidg*' | sort -V)

  if (( ${#nodes[@]} < 2 )); then
    return 1
  fi

  KEYBOARD_OUT="${nodes[0]}"
  MOUSE_OUT="${nodes[1]}"
  return 0
}

udev_prop() {
  local dev="$1"
  local key="$2"
  udevadm info --query=property --name="${dev}" 2>/dev/null | sed -n "s/^${key}=//p" | head -n 1
}

humanize_udev_value() {
  local value="$1"
  value="${value//_/ }"
  printf '%s' "${value}"
}

find_first_usb_mouse_event() {
  local dev
  for dev in /dev/input/event*; do
    [[ -e "${dev}" ]] || continue
    if [[ "$(udev_prop "${dev}" ID_INPUT_MOUSE)" == "1" && "$(udev_prop "${dev}" ID_BUS)" == "usb" ]]; then
      echo "${dev}"
      return 0
    fi
  done
  return 1
}

find_first_mouse_event() {
  find_first_usb_mouse_event && return 0

  local dev
  for dev in /dev/input/event*; do
    [[ -e "${dev}" ]] || continue
    if [[ "$(udev_prop "${dev}" ID_INPUT_MOUSE)" == "1" ]]; then
      echo "${dev}"
      return 0
    fi
  done
  return 1
}

find_first_keyboard_event() {
  local dev
  for dev in /dev/input/event*; do
    [[ -e "${dev}" ]] || continue
    if [[ "$(udev_prop "${dev}" ID_INPUT_KEYBOARD)" == "1" && "$(udev_prop "${dev}" ID_BUS)" == "usb" ]]; then
      echo "${dev}"
      return 0
    fi
  done

  for dev in /dev/input/event*; do
    [[ -e "${dev}" ]] || continue
    if [[ "$(udev_prop "${dev}" ID_INPUT_KEYBOARD)" == "1" ]]; then
      echo "${dev}"
      return 0
    fi
  done
  return 1
}

auto_spoof_from_mouse() {
  local event_dev
  event_dev="$(find_first_usb_mouse_event)" || {
    echo "No USB mouse event device found." >&2
    exit 1
  }

  local vendor_id model_id vendor_name product_name serial_short serial_full
  vendor_id="$(udev_prop "${event_dev}" ID_VENDOR_ID)"
  model_id="$(udev_prop "${event_dev}" ID_MODEL_ID)"
  vendor_name="$(udev_prop "${event_dev}" ID_VENDOR_FROM_DATABASE)"
  product_name="$(udev_prop "${event_dev}" ID_MODEL_FROM_DATABASE)"
  serial_short="$(udev_prop "${event_dev}" ID_SERIAL_SHORT)"
  serial_full="$(udev_prop "${event_dev}" ID_SERIAL)"

  if [[ -z "${vendor_name}" ]]; then
    vendor_name="$(udev_prop "${event_dev}" ID_VENDOR)"
  fi
  if [[ -z "${product_name}" ]]; then
    product_name="$(udev_prop "${event_dev}" ID_MODEL)"
  fi
  if [[ -z "${serial_short}" ]]; then
    serial_short="${serial_full}"
  fi
  if [[ -z "${serial_short}" ]]; then
    serial_short="AUTO-MOUSE-001"
  fi

  if [[ -z "${vendor_id}" || -z "${model_id}" ]]; then
    echo "Failed to read VID/PID from ${event_dev}" >&2
    exit 1
  fi

  USB_VID="0x${vendor_id}"
  USB_PID="0x${model_id}"
  USB_MANUFACTURER="$(humanize_udev_value "${vendor_name}")"
  USB_PRODUCT="$(humanize_udev_value "${product_name}")"
  USB_SERIAL="${serial_short}"
  DETECTED_MOUSE_EVENT="${event_dev}"
}

show_mouse_identity() {
  require_root
  auto_spoof_from_mouse
  cat <<EOF
Detected mouse event: ${DETECTED_MOUSE_EVENT}
USB_VID=${USB_VID}
USB_PID=${USB_PID}
USB_MANUFACTURER=${USB_MANUFACTURER}
USB_PRODUCT=${USB_PRODUCT}
USB_SERIAL=${USB_SERIAL}
EOF
}

setup_gadget() {
  require_root
  mount_if_needed configfs /sys/kernel/config
  load_gadget_support
  unbind_other_gadgets

  if [[ -d "${GADGET_DIR}" ]]; then
    teardown_gadget_internal
  fi

  mkdir -p "${GADGET_DIR}"
  cd "${GADGET_DIR}"

  echo "${USB_VID}" > idVendor
  echo "${USB_PID}" > idProduct
  echo "${USB_BCD_USB}" > bcdUSB
  echo "${USB_BCD_DEVICE}" > bcdDevice

  mkdir -p strings/0x409
  echo "${USB_SERIAL}" > strings/0x409/serialnumber
  echo "${USB_MANUFACTURER}" > strings/0x409/manufacturer
  echo "${USB_PRODUCT}" > strings/0x409/product

  mkdir -p configs/c.1/strings/0x409
  echo "Keyboard and Mouse" > configs/c.1/strings/0x409/configuration
  echo 250 > configs/c.1/MaxPower

  mkdir -p functions/hid.keyboard
  echo 1 > functions/hid.keyboard/protocol
  echo 1 > functions/hid.keyboard/subclass
  echo 8 > functions/hid.keyboard/report_length
  if [[ -e functions/hid.keyboard/interval ]]; then
    echo 1 > functions/hid.keyboard/interval
  fi
  printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x01\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x01\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > functions/hid.keyboard/report_desc

  mkdir -p functions/hid.mouse
  echo 2 > functions/hid.mouse/protocol
  echo 1 > functions/hid.mouse/subclass
  echo 4 > functions/hid.mouse/report_length
  if [[ -e functions/hid.mouse/interval ]]; then
    echo 1 > functions/hid.mouse/interval
  fi
  printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x01\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' > functions/hid.mouse/report_desc

  ln -s functions/hid.keyboard configs/c.1/
  ln -s functions/hid.mouse configs/c.1/

  local udc_name
  udc_name="$(ls /sys/class/udc | head -n 1)"
  if [[ -z "${udc_name}" ]]; then
    echo "No UDC controller found in /sys/class/udc" >&2
    exit 1
  fi

  echo "${udc_name}" > UDC
  sleep 1

  if ! resolve_hid_nodes; then
    echo "Gadget bound, but fewer than two hidg character devices are present." >&2
    exit 1
  fi

  chmod 666 "${KEYBOARD_OUT}" "${MOUSE_OUT}" 2>/dev/null || true

  echo "Gadget ready:"
  echo "VID:PID=${USB_VID}:${USB_PID}"
  echo "Manufacturer: ${USB_MANUFACTURER}"
  echo "Product: ${USB_PRODUCT}"
  ls -l "${KEYBOARD_OUT}" "${MOUSE_OUT}"
}

is_running() {
  if [[ ! -f "${PID_FILE}" ]]; then
    return 1
  fi

  local pid
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null
}

start_passthrough() {
  require_root
  ensure_evdev
  switch_to_device_mode
  setup_gadget

  if is_running; then
    echo "Passthrough already running. PID=$(cat "${PID_FILE}")"
    exit 0
  fi

  local py_bin
  py_bin="$(choose_python)"

  local keyboard_event mouse_event
  keyboard_event="${KEYBOARD_EVENT_PATH:-$(find_first_keyboard_event || true)}"
  mouse_event="${MOUSE_EVENT_PATH:-$(find_first_mouse_event || true)}"

  local cmd=(
    "${py_bin}" "${APP_PY}"
    --grab
    --scan-interval "${SCAN_INTERVAL}"
    --keyboard-out "${KEYBOARD_OUT}"
    --mouse-out "${MOUSE_OUT}"
  )
  if [[ -n "${keyboard_event}" ]]; then
    cmd+=(--keyboard-path "${keyboard_event}")
  fi
  if [[ -n "${mouse_event}" ]]; then
    cmd+=(--mouse-path "${mouse_event}")
  fi

  : > "${LOG_FILE}"
  nohup "${cmd[@]}" >> "${LOG_FILE}" 2>&1 &

  echo $! > "${PID_FILE}"
  sleep 1

  if ! is_running; then
    echo "Passthrough failed to start. Check log: ${LOG_FILE}" >&2
    exit 1
  fi

  echo "Passthrough started. PID=$(cat "${PID_FILE}")"
  echo "Keyboard event: ${keyboard_event:-auto-scan}"
  echo "Mouse event: ${mouse_event:-auto-scan}"
  echo "Keyboard out: ${KEYBOARD_OUT}"
  echo "Mouse out: ${MOUSE_OUT}"
  echo "VID:PID=${USB_VID}:${USB_PID}"
  echo "Log: ${LOG_FILE}"
}

stop_passthrough() {
  require_root

  if is_running; then
    local pid
    pid="$(cat "${PID_FILE}")"
    kill "${pid}" 2>/dev/null || true

    for _ in $(seq 1 20); do
      if ! kill -0 "${pid}" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done

    if kill -0 "${pid}" 2>/dev/null; then
      kill -9 "${pid}" 2>/dev/null || true
    fi
  fi

  rm -f "${PID_FILE}"
  teardown_gadget_internal
  echo "Passthrough stopped."
}

status_passthrough() {
  local mode_node
  mode_node="$(find_mode_node || true)"

  if is_running; then
    echo "Passthrough: running, PID=$(cat "${PID_FILE}")"
  else
    echo "Passthrough: stopped"
  fi

  if [[ -n "${mode_node}" && -e "${mode_node}" ]]; then
    echo "Mode node: ${mode_node}"
    echo "USB mode: $(read_mode "${mode_node}")"
  else
    echo "Mode node: not found"
  fi

  if resolve_hid_nodes; then
    echo "HID gadget: ready"
    echo "Keyboard out: ${KEYBOARD_OUT}"
    echo "Mouse out: ${MOUSE_OUT}"
    echo "VID:PID=${USB_VID}:${USB_PID}"
    echo "Manufacturer: ${USB_MANUFACTURER}"
    echo "Product: ${USB_PRODUCT}"
    ls -l "${KEYBOARD_OUT}" "${MOUSE_OUT}"
  else
    echo "HID gadget: missing"
  fi

  echo "Log file: ${LOG_FILE}"
}

list_devices() {
  require_root
  ensure_evdev
  local py_bin
  py_bin="$(choose_python)"
  "${py_bin}" "${APP_PY}" --list
}

start_auto_mouse() {
  require_root
  auto_spoof_from_mouse
  echo "Auto spoof from mouse: ${DETECTED_MOUSE_EVENT}"
  echo "VID:PID=${USB_VID}:${USB_PID}"
  echo "Manufacturer: ${USB_MANUFACTURER}"
  echo "Product: ${USB_PRODUCT}"
  start_passthrough
}

case "${ACTION}" in
  start)
    start_passthrough
    ;;
  stop)
    stop_passthrough
    ;;
  restart)
    stop_passthrough
    start_passthrough
    ;;
  status)
    status_passthrough
    ;;
  list)
    list_devices
    ;;
  setup)
    switch_to_device_mode
    setup_gadget
    ;;
  teardown)
    stop_passthrough
    ;;
  show-mouse-id)
    show_mouse_identity
    ;;
  start-auto-mouse)
    start_auto_mouse
    ;;
  *)
    usage
    exit 1
    ;;
esac
