#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SCRIPT_DIR}/deploy_new_system.sh"
ACTION="${1:-install}"

if [[ ! -f "${TARGET}" ]]; then
  echo "Deploy script not found: ${TARGET}" >&2
  exit 1
fi

exec bash "${TARGET}" "${ACTION}"
