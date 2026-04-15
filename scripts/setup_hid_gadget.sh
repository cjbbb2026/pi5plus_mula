#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ${EUID} -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

bash "${SCRIPT_DIR}/km_passthrough.sh" setup