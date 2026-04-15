#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"

set_cpu_governor() {
  local governor="$1"
  local policy
  for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [[ -w "$policy/scaling_governor" ]] || continue
    echo "$governor" > "$policy/scaling_governor"
  done
}

set_cpu_max_freq() {
  local policy
  local maxf
  for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [[ -r "$policy/cpuinfo_max_freq" && -w "$policy/scaling_max_freq" ]] || continue
    maxf="$(cat "$policy/cpuinfo_max_freq")"
    echo "$maxf" > "$policy/scaling_max_freq"
  done
}

set_cpu_min_to_max() {
  local policy
  local maxf
  for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [[ -r "$policy/scaling_max_freq" && -w "$policy/scaling_min_freq" ]] || continue
    maxf="$(cat "$policy/scaling_max_freq")"
    echo "$maxf" > "$policy/scaling_min_freq"
  done
}

restore_cpu_min_freq() {
  local policy
  local minf
  for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [[ -r "$policy/cpuinfo_min_freq" && -w "$policy/scaling_min_freq" ]] || continue
    minf="$(cat "$policy/cpuinfo_min_freq")"
    echo "$minf" > "$policy/scaling_min_freq"
  done
}

set_npu_governor() {
  local governor="$1"
  local dev
  for dev in /sys/class/devfreq/*npu* /sys/class/devfreq/*rknpu*; do
    [[ -w "$dev/governor" ]] || continue
    echo "$governor" > "$dev/governor" || true
  done
}

set_npu_max_freq() {
  local dev
  local maxf
  for dev in /sys/class/devfreq/*npu* /sys/class/devfreq/*rknpu*; do
    [[ -r "$dev/max_freq" && -w "$dev/min_freq" ]] || continue
    maxf="$(cat "$dev/max_freq")"
    echo "$maxf" > "$dev/min_freq" || true
  done
}

restore_npu_min_freq() {
  local dev
  local minf
  for dev in /sys/class/devfreq/*npu* /sys/class/devfreq/*rknpu*; do
    [[ -r "$dev/available_frequencies" && -w "$dev/min_freq" ]] || continue
    minf="$(tr ' ' '\n' < "$dev/available_frequencies" | sed '/^$/d' | head -n 1)"
    [[ -n "$minf" ]] && echo "$minf" > "$dev/min_freq" || true
  done
}

show_status() {
  echo "[CPU]"
  for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [[ -d "$policy" ]] || continue
    printf "%s cpus=%s gov=%s cur=%s min=%s max=%s\n"       "$(basename "$policy")"       "$(cat "$policy/affected_cpus" 2>/dev/null || echo '?')"       "$(cat "$policy/scaling_governor" 2>/dev/null || echo '?')"       "$(cat "$policy/scaling_cur_freq" 2>/dev/null || echo '?')"       "$(cat "$policy/scaling_min_freq" 2>/dev/null || echo '?')"       "$(cat "$policy/scaling_max_freq" 2>/dev/null || echo '?')"
  done
  echo
  echo "[NPU]"
  local found=0
  local dev
  for dev in /sys/class/devfreq/*npu* /sys/class/devfreq/*rknpu*; do
    [[ -d "$dev" ]] || continue
    found=1
    printf "%s gov=%s cur=%s min=%s max=%s\n"       "$(basename "$dev")"       "$(cat "$dev/governor" 2>/dev/null || echo '?')"       "$(cat "$dev/cur_freq" 2>/dev/null || echo '?')"       "$(cat "$dev/min_freq" 2>/dev/null || echo '?')"       "$(cat "$dev/max_freq" 2>/dev/null || echo '?')"
  done
  [[ "$found" -eq 1 ]] || echo "no npu devfreq node found"
}

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run with sudo: sudo bash scripts/perf_mode.sh $ACTION" >&2
    exit 1
  fi
}

case "$ACTION" in
  status)
    show_status
    ;;
  on)
    need_root
    set_cpu_max_freq
    set_cpu_governor performance
    set_cpu_min_to_max
    set_npu_governor performance
    set_npu_max_freq
    echo "Performance mode enabled."
    show_status
    ;;
  off)
    need_root
    restore_cpu_min_freq
    set_cpu_governor ondemand
    set_npu_governor rknpu_ondemand
    restore_npu_min_freq
    echo "Performance mode disabled."
    show_status
    ;;
  *)
    echo "Usage: $0 [status|on|off]" >&2
    exit 1
    ;;
esac
