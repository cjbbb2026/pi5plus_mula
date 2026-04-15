#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"
MODEL_PATH="${1:-}"

choose_python() {
  if [[ -n "${PYTHON_BIN}" ]]; then
    if [[ "${PYTHON_BIN}" == */* ]]; then
      [[ -x "${PYTHON_BIN}" ]] && { echo "${PYTHON_BIN}"; return 0; }
    elif command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
      echo "${PYTHON_BIN}"
      return 0
    fi
  fi
  if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
    echo "${PROJECT_DIR}/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return 0
  fi
  echo "python3 not found." >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  bash scripts/check_rknn_runtime.sh /path/to/model.rknn
EOF
}

[[ -n "${MODEL_PATH}" ]] || { usage; exit 1; }
[[ -f "${MODEL_PATH}" ]] || { echo "Model not found: ${MODEL_PATH}" >&2; exit 1; }

PYTHON_BIN="$(choose_python)"

"${PYTHON_BIN}" - "${MODEL_PATH}" <<'PY'
import sys
from pathlib import Path

model_path = Path(sys.argv[1]).expanduser().resolve()

try:
    from rknnlite.api import RKNNLite
except Exception as exc:
    raise SystemExit(f"RKNN Lite Python package is unavailable: {exc}")

rknn = RKNNLite()
ret = rknn.load_rknn(str(model_path))
if int(ret) != 0:
    raise SystemExit(f"load_rknn failed: {ret}")

core_auto = getattr(RKNNLite, "NPU_CORE_AUTO", None)
ret = rknn.init_runtime() if core_auto is None else rknn.init_runtime(core_mask=core_auto)
release = getattr(rknn, "release", None)
if callable(release):
    try:
        release()
    except Exception:
        pass

if int(ret) == 0:
    print(f"RKNN runtime check passed: {model_path}")
    raise SystemExit(0)

try:
    import ctypes
    librknnrt = ctypes.CDLL("librknnrt.so")
    get_sdk_version = getattr(librknnrt, "rknn_get_sdk_version", None)
except Exception:
    get_sdk_version = None

message = (
    f"RKNN runtime is incompatible with model: {model_path}\n"
    f"Current board runtime/driver is too old for this .rknn model, or the model was exported by a newer toolkit.\n"
    f"Typical symptom: 'Invalid RKNN model version'.\n"
    f"Use a newer board-side RKNN runtime, or re-export the model with a toolkit version compatible with this board."
)
raise SystemExit(message)
PY
