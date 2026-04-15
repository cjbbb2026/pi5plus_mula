#!/usr/bin/env bash
set -euo pipefail

INTERVAL="${1:-0.5}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="${AI_STATE_FILE:-$ROOT_DIR/.ai_state.json}"

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

read_state_pid() {
  if [[ -f "$STATE_FILE" ]]; then
    python3 - "$STATE_FILE" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

pid = (((data or {}).get("service") or {}).get("pid"))
if pid:
    print(pid)
PY
  fi
}

print_cpu_info() {
  local policy
  for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [[ -d "$policy" ]] || continue
    printf "%s cpus=%s gov=%s cur=%s min=%s max=%s\n"       "$(basename "$policy")"       "$(cat "$policy/affected_cpus" 2>/dev/null || echo '?')"       "$(cat "$policy/scaling_governor" 2>/dev/null || echo '?')"       "$(cat "$policy/scaling_cur_freq" 2>/dev/null || echo '?')"       "$(cat "$policy/scaling_min_freq" 2>/dev/null || echo '?')"       "$(cat "$policy/scaling_max_freq" 2>/dev/null || echo '?')"
  done
}

print_npu_info() {
  local found=0
  local dev
  for dev in /sys/class/devfreq/*npu* /sys/class/devfreq/*rknpu*; do
    [[ -d "$dev" ]] || continue
    found=1
    printf "%s gov=%s cur=%s min=%s max=%s avail=%s\n"       "$(basename "$dev")"       "$(cat "$dev/governor" 2>/dev/null || echo '?')"       "$(cat "$dev/cur_freq" 2>/dev/null || echo '?')"       "$(cat "$dev/min_freq" 2>/dev/null || echo '?')"       "$(cat "$dev/max_freq" 2>/dev/null || echo '?')"       "$(tr '\n' ' ' < "$dev/available_frequencies" 2>/dev/null || echo '?')"
  done
  if [[ "$found" -eq 0 ]]; then
    echo "no npu devfreq node found"
  fi
}

print_temps() {
  local zone
  local raw
  for zone in /sys/class/thermal/thermal_zone*; do
    [[ -f "$zone/type" ]] || continue
    raw="$(cat "$zone/temp" 2>/dev/null || echo '')"
    if [[ "$raw" =~ ^[0-9]+$ ]]; then
      awk -v type="$(cat "$zone/type" 2>/dev/null)" -v temp="$raw" 'BEGIN { printf "%s %.1fC\n", type, temp / 1000.0 }'
    else
      printf "%s %s\n" "$(cat "$zone/type" 2>/dev/null || echo '?')" "${raw:-?}"
    fi
  done
}

print_ai_proc() {
  local pid="${AI_PID:-}"
  if [[ -z "$pid" ]]; then
    pid="$(read_state_pid)"
  fi
  if [[ -z "$pid" ]]; then
    pid="$(pgrep -fn 'python3 .*app/ai.py' || true)"
  fi
  if [[ -z "$pid" ]]; then
    echo "ai process: not found"
    return
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "ai process: stale pid=$pid"
    return
  fi
  if have_cmd ps; then
    ps -p "$pid" -o pid=,%cpu=,%mem=,etime=,stat=,comm= | awk '{printf "ai process: pid=%s cpu=%s%% mem=%s%% etime=%s stat=%s cmd=%s\n",$1,$2,$3,$4,$5,$6}'
  else
    echo "ai process: pid=$pid"
  fi
}

print_thermal_dmesg() {
  if have_cmd dmesg; then
    dmesg | grep -Ei 'thermal|throttle|cooling|rknpu|npu' | tail -n 5 || true
  fi
}

while true; do
  clear || true
  echo "orangepi performance watch  interval=${INTERVAL}s  time=$(date '+%F %T')"
  echo
  echo "[CPU cpufreq]"
  print_cpu_info
  echo
  echo "[NPU devfreq]"
  print_npu_info
  echo
  echo "[Temperature]"
  print_temps
  echo
  echo "[AI process]"
  print_ai_proc
  echo
  echo "[Recent thermal/NPU dmesg]"
  print_thermal_dmesg
  sleep "$INTERVAL"
done
