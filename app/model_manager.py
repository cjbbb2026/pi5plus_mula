from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from runtime_config import runtime_defaults, sanitize_runtime_patch, sanitize_runtime_updates

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_STORE = ROOT / '.ai_models.json'
DEFAULT_MODEL_DIR = ROOT / 'models'
DEFAULT_CONFIG_DIR = ROOT / 'config'
DEFAULT_LAST_MODEL_FILE = ROOT / '.ai_last_model.txt'
_RUNTIME_VALIDATE_TIMEOUT = 30
MODEL_PROFILE_KEYS = {
    'model_label',
    'model_class_names_text',
    'model_notes',
    'model_uploaded',
    'model_created_at',
    'model_updated_at',
}
STORE_MODEL_KEYS = {
    'id',
    'path',
    'filename',
    'uploaded',
    'exists',
    'size',
    'sha256',
    'input_width',
    'input_height',
    'input_source',
    'detected_class_count',
    'valid',
    'validation_message',
    'runtime_valid',
    'runtime_message',
    'runtime_output_shapes',
    'runtime_output_count',
}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(path.parent), prefix=path.name + '.', suffix='.tmp', delete=False) as fp:
        fp.write(body)
        temp_name = fp.name
    os.replace(temp_name, path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(path.parent), prefix=path.name + '.', suffix='.tmp', delete=False) as fp:
        fp.write(text)
        temp_name = fp.name
    os.replace(temp_name, path)


def infer_model_input_hw(model_path: Path) -> tuple[int, int]:
    model_name = model_path.name.lower()
    for candidate in (1280, 960, 800, 768, 736, 672, 640, 512, 416, 320, 256):
        if str(candidate) in model_name:
            return candidate, candidate
    return 640, 640


def _attr_value(attr: Any, key: str) -> Any:
    if isinstance(attr, dict):
        return attr.get(key)
    return getattr(attr, key, None)


def _dims_to_hw(dims: Any, fmt: Any) -> tuple[int, int] | None:
    if dims is None:
        return None
    try:
        values = [int(v) for v in list(dims)]
    except Exception:
        return None
    if len(values) < 2:
        return None
    if len(values) == 4:
        if values[0] == 1 and values[1] == 3:
            return values[2], values[3]
        if values[0] == 1 and values[3] == 3:
            return values[1], values[2]
    fmt_text = str(fmt).upper() if fmt is not None else ''
    if 'NHWC' in fmt_text and len(values) >= 4:
        return values[1], values[2]
    if 'NCHW' in fmt_text and len(values) >= 4:
        return values[2], values[3]
    positive = [v for v in values if v > 1]
    if len(positive) >= 2:
        return positive[-2], positive[-1]
    return None


def _dims_to_shape(dims: Any) -> tuple[int, ...]:
    try:
        return tuple(int(v) for v in list(dims))
    except Exception:
        return ()


def _infer_class_count_from_output_shapes(shapes: list[tuple[int, ...]]) -> int | None:
    candidates: list[int] = []
    for shape in shapes:
        positive = [int(v) for v in shape if int(v) > 1]
        if len(positive) < 2:
            continue
        for dim in positive:
            if 5 <= dim <= 1024:
                candidates.extend([dim - 4, dim - 5])
    filtered = [value for value in candidates if 1 <= value <= 1000]
    if not filtered:
        return None
    preferred = [value for value in filtered if value not in (79, 80, 81)]
    pool = preferred or filtered
    counts: dict[int, int] = {}
    for value in pool:
        counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], abs(item[0] - 80)))
    return ranked[0][0] if ranked else None


def _infer_input_hw_from_output_shapes(shapes: list[tuple[int, ...]]) -> tuple[int, int] | None:
    if not shapes:
        return None

    if len(shapes) == 1 and len(shapes[0]) == 3:
        shape = tuple(int(v) for v in shapes[0])
        points_to_size = {
            1344: 256,
            2100: 320,
            3549: 416,
            5376: 512,
            8400: 640,
            18900: 960,
            33600: 1280,
        }
        positive = [int(v) for v in shape if int(v) > 1]
        for dim in positive:
            if dim in points_to_size:
                size = points_to_size[int(dim)]
                return size, size

    grid_candidates: list[int] = []
    for shape in shapes:
        positive = [int(v) for v in shape if int(v) > 1]
        if len(positive) < 3:
            continue
        if len(shape) >= 4:
            h = int(shape[-2])
            w = int(shape[-1])
            if h > 0 and w > 0 and abs(h - w) <= 2:
                grid_candidates.append(max(h, w))
    if not grid_candidates:
        return None

    size = max(grid_candidates) * 8
    if size <= 0:
        return None
    return int(size), int(size)


def split_class_names(value: Any) -> list[str]:
    if isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        text = str(value or '')
        for sep in ('\r', ',', '\uff0c', ';', '\uff1b'):
            text = text.replace(sep, '\n')
        items = [part.strip() for part in text.split('\n')]
    return [item for item in items if item]


def class_names_to_text(class_names: list[str]) -> str:
    return '\n'.join(str(name).strip() for name in class_names if str(name).strip())


def normalize_key_text(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    normalized = (
        text.replace('\uff0c', ',')
        .replace('\uff1b', ',')
        .replace(';', ',')
        .replace('\n', ',')
        .replace('\r', ',')
    )
    parts = [part.strip().upper() for part in re.split(r'[\s,]+', normalized) if part.strip()]
    unique: list[str] = []
    for part in parts:
        if part not in unique:
            unique.append(part)
    return ','.join(unique)


def normalize_target_hotkeys(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        try:
            class_id = max(0, int(float(raw_key)))
        except Exception:
            continue
        key_text = normalize_key_text(raw_value)
        if key_text:
            normalized[str(class_id)] = key_text
    return normalized


def normalize_runtime_overrides(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    clean = sanitize_runtime_updates(value)
    defaults = runtime_defaults()
    return {
        name: clean_value
        for name, clean_value in clean.items()
        if defaults.get(name) != clean_value
    }


def parse_target_hotkeys_text(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return normalize_target_hotkeys(value)
    text = str(value or '').strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return normalize_target_hotkeys(parsed)


def normalize_model_id(name: str) -> str:
    text = re.sub(r'[^a-zA-Z0-9._-]+', '-', name.strip())
    text = text.strip('-._').lower()
    return text or f'model-{int(time.time())}'


def ensure_unique_filename(target_dir: Path, filename: str) -> Path:
    safe_name = Path(filename).name
    stem = Path(safe_name).stem or 'model'
    suffix = Path(safe_name).suffix or '.rknn'
    candidate = target_dir / f'{stem}{suffix}'
    index = 1
    while candidate.exists():
        candidate = target_dir / f'{stem}-{index}{suffix}'
        index += 1
    return candidate


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _runtime_validate(path: Path) -> dict[str, Any]:
    script = r"""
import json
import sys
from pathlib import Path


def attr_value(attr, key):
    if isinstance(attr, dict):
        return attr.get(key)
    return getattr(attr, key, None)


def dims_to_hw(dims, fmt):
    if dims is None:
        return None
    try:
        values = [int(v) for v in list(dims)]
    except Exception:
        return None
    if len(values) < 2:
        return None
    if len(values) == 4:
        if values[0] == 1 and values[1] == 3:
            return values[2], values[3]
        if values[0] == 1 and values[3] == 3:
            return values[1], values[2]
    fmt_text = str(fmt).upper() if fmt is not None else ''
    if 'NHWC' in fmt_text and len(values) >= 4:
        return values[1], values[2]
    if 'NCHW' in fmt_text and len(values) >= 4:
        return values[2], values[3]
    positive = [v for v in values if v > 1]
    if len(positive) >= 2:
        return positive[-2], positive[-1]
    return None


def dims_to_shape(dims):
    try:
        return tuple(int(v) for v in list(dims))
    except Exception:
        return ()


def infer_class_count(shapes):
    candidates = []
    for shape in shapes:
        positive = [int(v) for v in shape if int(v) > 1]
        if len(positive) < 2:
            continue
        for dim in positive:
            if 5 <= dim <= 1024:
                candidates.extend([dim - 4, dim - 5])
    filtered = [value for value in candidates if 1 <= value <= 1000]
    if not filtered:
        return None
    preferred = [value for value in filtered if value not in (79, 80, 81)]
    pool = preferred or filtered
    counts = {}
    for value in pool:
        counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], abs(item[0] - 80)))
    return ranked[0][0] if ranked else None


path = Path(sys.argv[1])
result = {
    'runtime_valid': False,
    'runtime_message': 'unknown',
    'runtime_input_width': None,
    'runtime_input_height': None,
    'runtime_output_shapes': [],
    'runtime_output_count': 0,
    'runtime_class_count': None,
}
try:
    from rknnlite.api import RKNNLite
except Exception as exc:
    result['runtime_message'] = f'rknnlite unavailable: {exc}'
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0)

rknn = RKNNLite()
try:
    ret = rknn.load_rknn(str(path))
    if int(ret) != 0:
        result['runtime_message'] = f'load failed: {ret}'
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0)

    init_ret = rknn.init_runtime()
    result['runtime_valid'] = int(init_ret) == 0
    result['runtime_message'] = 'runtime ok' if int(init_ret) == 0 else f'init_runtime failed: {init_ret}'

    if result['runtime_valid']:
        input_query = getattr(RKNNLite, 'RKNN_QUERY_INPUT_ATTR', None)
        if input_query is not None and hasattr(rknn, 'query'):
            try:
                input_attr = rknn.query(input_query, 0)
                hw = dims_to_hw(attr_value(input_attr, 'dims'), attr_value(input_attr, 'fmt'))
                if hw is not None:
                    result['runtime_input_height'], result['runtime_input_width'] = hw
            except Exception:
                pass

        output_query = getattr(RKNNLite, 'RKNN_QUERY_OUTPUT_ATTR', None)
        if output_query is not None and hasattr(rknn, 'query'):
            shapes = []
            for index in range(16):
                try:
                    output_attr = rknn.query(output_query, index)
                except Exception:
                    break
                shape = dims_to_shape(attr_value(output_attr, 'dims'))
                if not shape:
                    break
                shapes.append(shape)
            result['runtime_output_shapes'] = shapes
            result['runtime_output_count'] = len(shapes)
            result['runtime_class_count'] = infer_class_count(shapes)
except Exception as exc:
    result['runtime_message'] = str(exc)
finally:
    release = getattr(rknn, 'release', None)
    if callable(release):
        try:
            release()
        except Exception:
            pass
print(json.dumps(result, ensure_ascii=False))
"""
    completed = subprocess.run(
        [sys.executable, '-c', script, str(path)],
        capture_output=True,
        text=True,
        timeout=_RUNTIME_VALIDATE_TIMEOUT,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        stderr = (completed.stderr or '').strip()
        return {'runtime_valid': False, 'runtime_message': stderr or 'validation produced no output'}
    try:
        data = json.loads(lines[-1])
    except Exception:
        return {'runtime_valid': False, 'runtime_message': lines[-1]}
    if not isinstance(data, dict):
        return {'runtime_valid': False, 'runtime_message': f'unexpected validation output: {data!r}'}
    return data


def validate_model_file(path: Path, check_runtime: bool = True) -> dict[str, Any]:
    path = path.expanduser().resolve()
    result: dict[str, Any] = {
        'path': str(path),
        'exists': path.exists(),
        'filename': path.name,
        'size': 0,
        'sha256': '',
        'input_width': None,
        'input_height': None,
        'input_source': 'unknown',
        'detected_class_count': None,
        'valid': False,
        'validation_message': '',
        'runtime_valid': None,
        'runtime_message': '',
        'runtime_output_shapes': [],
        'runtime_output_count': 0,
    }
    if not path.exists():
        result['validation_message'] = 'file missing'
        return result
    result['size'] = path.stat().st_size
    result['sha256'] = file_sha256(path)
    input_h, input_w = infer_model_input_hw(path)
    result['input_width'] = input_w
    result['input_height'] = input_h
    result['input_source'] = 'filename-fallback'
    if path.suffix.lower() != '.rknn':
        result['validation_message'] = 'only .rknn files are supported'
        return result
    if result['size'] <= 4096:
        result['validation_message'] = 'file is too small to be a valid RKNN model'
        return result
    result['valid'] = True
    result['validation_message'] = 'basic validation ok'
    if check_runtime:
        try:
            runtime = _runtime_validate(path)
        except Exception as exc:
            runtime = {'runtime_valid': False, 'runtime_message': f'validation exception: {exc}'}
        result['runtime_valid'] = bool(runtime.get('runtime_valid'))
        result['runtime_message'] = str(runtime.get('runtime_message', ''))
        result['runtime_output_shapes'] = list(runtime.get('runtime_output_shapes') or [])
        result['runtime_output_count'] = int(runtime.get('runtime_output_count') or 0)
        runtime_input_width = runtime.get('runtime_input_width')
        runtime_input_height = runtime.get('runtime_input_height')
        if runtime_input_width and runtime_input_height:
            result['input_width'] = int(runtime_input_width)
            result['input_height'] = int(runtime_input_height)
            result['input_source'] = 'runtime'
        else:
            inferred_hw = _infer_input_hw_from_output_shapes(result['runtime_output_shapes'])
            if inferred_hw is not None:
                result['input_height'], result['input_width'] = inferred_hw
                result['input_source'] = 'runtime-output-shape'
        runtime_class_count = runtime.get('runtime_class_count')
        if runtime_class_count is not None:
            try:
                result['detected_class_count'] = max(0, int(runtime_class_count))
            except Exception:
                result['detected_class_count'] = None
        if result['runtime_valid']:
            result['validation_message'] = 'runtime validation ok'
        else:
            result['valid'] = False
            result['validation_message'] = result['runtime_message'] or 'runtime validation failed'
    return result


class ModelStore:
    def __init__(
        self,
        store_path: Path | None = None,
        model_dir: Path | None = None,
        root_dir: Path | None = None,
        config_dir: Path | None = None,
        last_model_file: Path | None = None,
    ) -> None:
        self.root_dir = (root_dir or ROOT).expanduser().resolve()
        self.store_path = (store_path or DEFAULT_MODEL_STORE).expanduser().resolve()
        self.model_dir = (model_dir or DEFAULT_MODEL_DIR).expanduser().resolve()
        self.config_dir = (config_dir or DEFAULT_CONFIG_DIR).expanduser().resolve()
        self.last_model_file = (last_model_file or DEFAULT_LAST_MODEL_FILE).expanduser().resolve()
        self._lock = threading.RLock()


    def _is_model_in_managed_dir(self, model_path: Path) -> bool:
        try:
            resolved = model_path.expanduser().resolve()
        except Exception:
            return False
        return resolved == self.model_dir or self.model_dir in resolved.parents

    def _empty(self) -> dict[str, Any]:
        return {'selected_model_id': '', 'models': {}}

    def model_config_path(self, model_id: str) -> Path:
        safe_id = normalize_model_id(str(model_id or '').strip())
        return self.config_dir / f'{safe_id}.json'

    def _read_model_config_raw(self, model_id: str) -> dict[str, Any]:
        path = self.model_config_path(model_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _profile_payload_from_model(self, model: dict[str, Any]) -> dict[str, Any]:
        class_names_text = str(
            model.get('class_names_text')
            or class_names_to_text(split_class_names(model.get('class_names') or ''))
            or ''
        )
        return {
            'model_label': str(model.get('label') or model.get('id') or '').strip(),
            'model_class_names_text': class_names_text,
            'model_notes': str(model.get('notes') or ''),
            'model_uploaded': bool(model.get('uploaded', False)),
            'model_created_at': float(model.get('created_at') or time.time()),
            'model_updated_at': float(model.get('updated_at') or time.time()),
        }

    def _config_updates_from_model_entry(self, model: dict[str, Any]) -> dict[str, Any]:
        updates = self._profile_payload_from_model(model)
        updates['aim_class'] = int(model.get('aim_class', 0) or 0)
        updates['aim_keys_text'] = normalize_key_text(model.get('aim_keys_text') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT'
        target_hotkeys = normalize_target_hotkeys(model.get('target_hotkeys'))
        updates['aim_target_keys_text'] = json.dumps(target_hotkeys, ensure_ascii=False, sort_keys=True) if target_hotkeys else ''
        return updates

    def _profile_payload_from_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return {key: config[key] for key in MODEL_PROFILE_KEYS if key in config}

    def _entry_profile_from_config(self, model_id: str) -> dict[str, Any]:
        config = self._read_model_config_raw(model_id)
        if not config:
            return {}
        profile: dict[str, Any] = {}
        label = str(config.get('model_label') or '').strip()
        if label:
            profile['label'] = label
        class_names_text = str(config.get('model_class_names_text') or '').strip()
        if class_names_text:
            profile['class_names_text'] = class_names_text
            profile['class_names'] = split_class_names(class_names_text)
        notes = str(config.get('model_notes') or '').strip()
        if notes:
            profile['notes'] = notes
        if 'model_uploaded' in config:
            profile['uploaded'] = bool(config.get('model_uploaded'))
        if 'model_created_at' in config:
            try:
                profile['created_at'] = float(config.get('model_created_at') or 0.0)
            except Exception:
                pass
        if 'model_updated_at' in config:
            try:
                profile['updated_at'] = float(config.get('model_updated_at') or 0.0)
            except Exception:
                pass
        if 'aim_class' in config:
            try:
                profile['aim_class'] = max(0, int(float(config.get('aim_class') or 0)))
            except Exception:
                pass
        if 'aim_keys_text' in config:
            profile['aim_keys_text'] = normalize_key_text(config.get('aim_keys_text') or '') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT'
        if 'aim_target_keys_text' in config:
            profile['target_hotkeys'] = parse_target_hotkeys_text(config.get('aim_target_keys_text'))
        return profile

    def _apply_config_profile(self, model_id: str, model: dict[str, Any]) -> dict[str, Any]:
        merged = dict(model)
        profile = self._entry_profile_from_config(model_id)
        if profile:
            merged.update(profile)
        return merged

    def read_last_model_id(self) -> str:
        try:
            return self.last_model_file.read_text(encoding='utf-8-sig').strip()
        except Exception:
            return ''

    def write_last_model_id(self, model_id: str) -> None:
        model_id = str(model_id or '').strip()
        if not model_id:
            return
        try:
            atomic_write_text(self.last_model_file, model_id + '\n')
        except Exception:
            return

    def _resolve_last_model_id(self, models: dict[str, Any]) -> str:
        marker = self.read_last_model_id()
        if not marker:
            return ''
        if marker in models:
            return marker
        for model_id, raw in models.items():
            if not isinstance(raw, dict):
                continue
            path_text = str(raw.get('path') or '').strip()
            if marker == path_text:
                return str(model_id)
            try:
                if marker == str(Path(path_text).expanduser().resolve()):
                    return str(model_id)
            except Exception:
                pass
        return ''

    def default_runtime_config_for_model(self, model: dict[str, Any]) -> dict[str, Any]:
        config = runtime_defaults()
        runtime_overrides = model.get('runtime_overrides')
        if isinstance(runtime_overrides, dict):
            config = sanitize_runtime_patch(config, runtime_overrides)
        config['aim_class'] = int(model.get('aim_class', config.get('aim_class', 0)) or 0)
        config['aim_keys_text'] = normalize_key_text(
            model.get('aim_keys_text') or config.get('aim_keys_text') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT'
        ) or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT'
        target_hotkeys = normalize_target_hotkeys(model.get('target_hotkeys'))
        config['aim_target_keys_text'] = json.dumps(target_hotkeys, ensure_ascii=False, sort_keys=True) if target_hotkeys else ''
        runtime_config = sanitize_runtime_patch(runtime_defaults(), config)
        runtime_config.update(self._profile_payload_from_model(model))
        return runtime_config

    def ensure_model_config(self, model_id_or_model: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(model_id_or_model, dict):
            model = dict(model_id_or_model)
            model_id = str(model.get('id') or '').strip()
        else:
            model_id = str(model_id_or_model or '').strip()
            model = self.get_model(model_id, check_runtime=False)
        if not model_id:
            raise ValueError('model id is required')
        path = self.model_config_path(model_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8-sig'))
            except Exception:
                data = {}
            if isinstance(data, dict):
                config = sanitize_runtime_patch(runtime_defaults(), data)
                config.update(self._profile_payload_from_config(data))
                profile_defaults = self._profile_payload_from_model(model)
                for key, value in profile_defaults.items():
                    config.setdefault(key, value)
                return config
        return self.default_runtime_config_for_model(model)

    def save_model_config(self, model_id: str, config: dict[str, Any]) -> dict[str, Any]:
        model_id = str(model_id or '').strip()
        if not model_id:
            raise ValueError('model id is required')
        existing = self._read_model_config_raw(model_id)
        profile = self._profile_payload_from_config(existing)
        profile.update(self._profile_payload_from_config(config))
        clean = sanitize_runtime_patch(runtime_defaults(), config)
        clean.update(profile)
        atomic_write_json(self.model_config_path(model_id), clean)
        return clean

    def selected_model_config_path(self) -> Path | None:
        payload = self.refresh(check_runtime=False)
        selected_id = str(payload.get('selected_model_id') or '')
        if not selected_id:
            return None
        model = payload.get('models', {}).get(selected_id)
        if not isinstance(model, dict):
            return None
        self.ensure_model_config(model)
        return self.model_config_path(selected_id)

    def load(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return self._empty()
        try:
            data = json.loads(self.store_path.read_text(encoding='utf-8-sig'))
        except Exception:
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        models = data.get('models', {})
        if not isinstance(models, dict):
            models = {}
        return {'selected_model_id': str(data.get('selected_model_id', '') or ''), 'models': models}

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        models = payload.get('models', {})
        cleaned_models: dict[str, Any] = {}
        if isinstance(models, dict):
            for model_id, raw in models.items():
                if not isinstance(raw, dict):
                    continue
                cleaned_models[str(model_id)] = {
                    key: raw[key]
                    for key in STORE_MODEL_KEYS
                    if key in raw
                }
        cleaned_payload = {
            'selected_model_id': str(payload.get('selected_model_id', '') or ''),
            'models': cleaned_models,
        }
        atomic_write_json(self.store_path, cleaned_payload)
        return cleaned_payload

    def _discovered_paths(self) -> list[Path]:
        paths: list[Path] = []
        search_specs: list[tuple[Path, str]] = [
            (self.root_dir, '*.rknn'),
            (self.model_dir, '**/*.rknn'),
        ]
        for base, pattern in search_specs:
            if not base.exists():
                continue
            for path in sorted(base.glob(pattern)):
                resolved = path.expanduser().resolve()
                if resolved not in paths:
                    paths.append(resolved)
        return paths

    def _entry_from_file(self, path: Path, existing: dict[str, Any] | None = None, uploaded: bool = False, check_runtime: bool = True) -> dict[str, Any]:
        validation = validate_model_file(path, check_runtime=check_runtime)
        existing = existing or {}
        class_names = split_class_names(existing.get('class_names') or existing.get('class_names_text') or '')
        if not check_runtime:
            if existing.get('input_source') == 'runtime' and existing.get('input_width') and existing.get('input_height'):
                validation['input_width'] = existing.get('input_width')
                validation['input_height'] = existing.get('input_height')
                validation['input_source'] = 'runtime'
            if existing.get('detected_class_count') is not None:
                validation['detected_class_count'] = existing.get('detected_class_count')
            for key in ('runtime_valid', 'runtime_message', 'runtime_output_shapes', 'runtime_output_count'):
                if key in existing:
                    validation[key] = existing.get(key)
            if existing.get('runtime_valid'):
                validation['valid'] = True
                validation['validation_message'] = str(existing.get('validation_message') or 'runtime validation ok')
        entry = {
            'id': str(existing.get('id') or normalize_model_id(path.stem)),
            'label': str(existing.get('label') or path.stem),
            'path': str(path),
            'filename': path.name,
            'uploaded': bool(existing.get('uploaded', uploaded)),
            'created_at': float(existing.get('created_at') or time.time()),
            'updated_at': float(existing.get('updated_at') or time.time()),
            'class_names': class_names,
            'class_names_text': str(existing.get('class_names_text') or class_names_to_text(class_names)),
            'aim_class': int(existing.get('aim_class', 0) or 0),
            'aim_keys_text': normalize_key_text(existing.get('aim_keys_text') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT',
            'target_hotkeys': normalize_target_hotkeys(existing.get('target_hotkeys')),
            'runtime_overrides': normalize_runtime_overrides(existing.get('runtime_overrides')),
            'notes': str(existing.get('notes') or ''),
        }
        entry.update(validation)
        return entry

    def refresh(self, check_runtime: bool = False) -> dict[str, Any]:
        with self._lock:
            payload = self.load()
            models = payload.get('models', {})
            selected = str(payload.get('selected_model_id') or '')

            def model_rank(raw: dict[str, Any]) -> tuple[int, int, int, int, int, int, float]:
                label = str(raw.get('label') or '').strip()
                path_text = str(raw.get('path') or '').strip()
                stem = Path(path_text).stem if path_text else ''
                class_names_text = str(raw.get('class_names_text') or '').strip()
                notes = str(raw.get('notes') or '').strip()
                hotkeys = normalize_target_hotkeys(raw.get('target_hotkeys'))
                overrides = normalize_runtime_overrides(raw.get('runtime_overrides'))
                return (
                    1 if bool(raw.get('uploaded')) else 0,
                    1 if label and label != stem else 0,
                    1 if bool(class_names_text) else 0,
                    1 if bool(notes) else 0,
                    1 if bool(hotkeys) else 0,
                    1 if bool(overrides) else 0,
                    float(raw.get('updated_at') or 0.0),
                )

            def merge_model_entries(preferred: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
                merged = dict(preferred)
                for key in ('label', 'class_names_text', 'notes'):
                    if not str(merged.get(key) or '').strip() and str(other.get(key) or '').strip():
                        merged[key] = other.get(key)
                if not merged.get('uploaded') and other.get('uploaded'):
                    merged['uploaded'] = True
                if not normalize_target_hotkeys(merged.get('target_hotkeys')) and normalize_target_hotkeys(other.get('target_hotkeys')):
                    merged['target_hotkeys'] = other.get('target_hotkeys')
                if not normalize_runtime_overrides(merged.get('runtime_overrides')) and normalize_runtime_overrides(other.get('runtime_overrides')):
                    merged['runtime_overrides'] = other.get('runtime_overrides')
                if not str(merged.get('aim_keys_text') or '').strip() and str(other.get('aim_keys_text') or '').strip():
                    merged['aim_keys_text'] = other.get('aim_keys_text')
                if int(merged.get('aim_class', 0) or 0) == 0 and int(other.get('aim_class', 0) or 0) != 0:
                    merged['aim_class'] = other.get('aim_class')
                merged['created_at'] = min(float(preferred.get('created_at') or 0.0), float(other.get('created_at') or 0.0)) or float(preferred.get('created_at') or other.get('created_at') or time.time())
                merged['updated_at'] = max(float(preferred.get('updated_at') or 0.0), float(other.get('updated_at') or 0.0))
                return merged

            path_owner: dict[str, str] = {}
            for model_id in list(models.keys()):
                raw = models.get(model_id)
                if not isinstance(raw, dict):
                    del models[model_id]
                    continue
                raw_path = str(raw.get('path') or '').strip()
                if not raw_path:
                    continue
                existing_id = path_owner.get(raw_path)
                if not existing_id or not isinstance(models.get(existing_id), dict):
                    path_owner[raw_path] = model_id
                    continue
                existing_raw = dict(models[existing_id])
                current_raw = dict(raw)
                if model_rank(current_raw) > model_rank(existing_raw):
                    keep_id, keep_raw = model_id, merge_model_entries(current_raw, existing_raw)
                    drop_id = existing_id
                else:
                    keep_id, keep_raw = existing_id, merge_model_entries(existing_raw, current_raw)
                    drop_id = model_id
                models[keep_id] = keep_raw
                if selected == drop_id:
                    selected = keep_id
                del models[drop_id]
                path_owner[raw_path] = keep_id

            discovered_paths = self._discovered_paths()
            known_paths: set[str] = set()
            for raw in models.values():
                if not isinstance(raw, dict):
                    continue
                raw_path = str(raw.get('path') or '').strip()
                if not raw_path:
                    continue
                try:
                    known_paths.add(str(Path(raw_path).expanduser().resolve()))
                except Exception:
                    known_paths.add(raw_path)

            for discovered_path in discovered_paths:
                discovered_text = str(discovered_path)
                if discovered_text in known_paths:
                    continue
                discovered_id = normalize_model_id(discovered_path.stem)
                base_id = discovered_id
                index = 1
                while discovered_id in models:
                    discovered_id = f'{base_id}-{index}'
                    index += 1
                models[discovered_id] = self._entry_from_file(
                    discovered_path,
                    existing={
                        'id': discovered_id,
                        'label': discovered_path.stem,
                        'uploaded': False,
                    },
                    uploaded=False,
                    check_runtime=check_runtime,
                )
                known_paths.add(discovered_text)

            for model_id, raw in list(models.items()):
                if not isinstance(raw, dict):
                    continue
                raw_path = str(raw.get('path') or '').strip()
                if not raw_path:
                    stale = dict(raw)
                    stale.update({'exists': False, 'valid': False, 'validation_message': 'file missing', 'runtime_valid': False, 'runtime_message': 'file missing'})
                    models[model_id] = stale
                    continue
                model_path = Path(raw_path).expanduser()
                if model_path.exists():
                    models[model_id] = self._entry_from_file(model_path, existing=raw, check_runtime=check_runtime)
                    continue
                stale = dict(raw)
                stale.update({'exists': False, 'valid': False, 'validation_message': 'file missing', 'runtime_valid': False, 'runtime_message': 'file missing'})
                models[model_id] = stale

            for model_id, raw in list(models.items()):
                if not isinstance(raw, dict):
                    del models[model_id]
                    continue
                model_path = Path(str(raw.get('path') or ''))
                if model_path.exists():
                    continue
                stale = dict(raw)
                stale.update({'exists': False, 'valid': False, 'validation_message': 'file missing', 'runtime_valid': False, 'runtime_message': 'file missing'})
                models[model_id] = stale

            sha_owner: dict[str, str] = {}
            for model_id in list(models.keys()):
                raw = models.get(model_id)
                if not isinstance(raw, dict):
                    continue
                raw_sha = str(raw.get('sha256') or '').strip().lower()
                if not raw_sha:
                    continue
                existing_id = sha_owner.get(raw_sha)
                if not existing_id or not isinstance(models.get(existing_id), dict):
                    sha_owner[raw_sha] = model_id
                    continue
                existing_raw = dict(models[existing_id])
                current_raw = dict(raw)
                if model_rank(current_raw) > model_rank(existing_raw):
                    keep_id, keep_raw = model_id, merge_model_entries(current_raw, existing_raw)
                    drop_id = existing_id
                else:
                    keep_id, keep_raw = existing_id, merge_model_entries(existing_raw, current_raw)
                    drop_id = model_id
                models[keep_id] = keep_raw
                if selected == drop_id:
                    selected = keep_id
                del models[drop_id]
                sha_owner[raw_sha] = keep_id

            payload['models'] = models
            last_selected = self._resolve_last_model_id(models)
            if last_selected:
                selected = last_selected
            payload['selected_model_id'] = selected
            if not selected or selected not in models:
                for model_id, raw in models.items():
                    if isinstance(raw, dict) and bool(raw.get('exists')):
                        payload['selected_model_id'] = model_id
                        break
            if payload.get('selected_model_id'):
                self.write_last_model_id(str(payload.get('selected_model_id') or ''))
            return self.save(payload)

    def list_payload(self, check_runtime: bool = False) -> dict[str, Any]:
        payload = self.refresh(check_runtime=check_runtime)
        models = payload.get('models', {})
        items = []
        selected_id = str(payload.get('selected_model_id') or '')
        for model_id in sorted(models, key=lambda mid: (mid != selected_id, str(models[mid].get('label') or mid).lower())):
            raw = models[model_id]
            if not isinstance(raw, dict):
                continue
            entry = self._apply_config_profile(model_id, raw)
            entry['selected'] = model_id == selected_id
            manual_class_count = len(split_class_names(entry.get('class_names') or entry.get('class_names_text') or ''))
            detected_class_count = entry.get('detected_class_count')
            try:
                detected_class_count = int(detected_class_count) if detected_class_count is not None else None
            except Exception:
                detected_class_count = None
            entry['manual_class_count'] = manual_class_count
            entry['class_count'] = detected_class_count if detected_class_count and detected_class_count > 0 else manual_class_count
            entry['class_count_source'] = 'runtime' if detected_class_count and detected_class_count > 0 else 'manual'
            entry['deletable'] = self._is_model_in_managed_dir(Path(str(entry.get('path') or '')))
            entry['target_hotkeys'] = normalize_target_hotkeys(entry.get('target_hotkeys'))
            entry['target_hotkey_count'] = len(entry['target_hotkeys'])
            entry['runtime_overrides'] = normalize_runtime_overrides(entry.get('runtime_overrides'))
            self.ensure_model_config(entry)
            entry['config_path'] = str(self.model_config_path(model_id))
            items.append(entry)
        selected = next((item for item in items if item.get('selected')), None)
        return {'selected_model_id': selected_id, 'selected_model': selected, 'models': items}

    def get_model(self, model_id: str, check_runtime: bool = False) -> dict[str, Any]:
        payload = self.refresh(check_runtime=check_runtime)
        raw = payload.get('models', {}).get(model_id)
        if not isinstance(raw, dict):
            raise KeyError(f'model not found: {model_id}')
        return self._apply_config_profile(model_id, raw)

    def save_uploaded_file(self, filename: str, content: bytes, preferred_path: Path | None = None) -> Path:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        if preferred_path is not None:
            preferred_path.parent.mkdir(parents=True, exist_ok=True)
            preferred_path.write_bytes(content)
            return preferred_path
        target = ensure_unique_filename(self.model_dir, filename)
        target.write_bytes(content)
        return target

    def register_uploaded_model(self, filename: str, content: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            metadata = metadata or {}
            payload = self.refresh(check_runtime=False)
        upload_sha = content_sha256(content).strip().lower()

        existing_model_id = ''
        existing_raw: dict[str, Any] | None = None
        existing_target: Path | None = None
        for model_id, raw in payload.get('models', {}).items():
            if not isinstance(raw, dict):
                continue
            raw_sha = str(raw.get('sha256') or '').strip().lower()
            raw_path = Path(str(raw.get('path') or '')).expanduser()
            if upload_sha and raw_sha == upload_sha and raw_path.exists():
                existing_model_id = model_id
                existing_raw = dict(raw)
                existing_target = raw_path.resolve()
                break

        if existing_target is None:
            target = self.save_uploaded_file(filename, content)
        else:
            target = existing_target

        target_path = str(target)
        if not existing_model_id:
            for model_id, raw in payload.get('models', {}).items():
                if not isinstance(raw, dict):
                    continue
                if str(raw.get('path') or '').strip() == target_path:
                    existing_model_id = model_id
                    existing_raw = dict(raw)
                    break

        if existing_model_id:
            model_id = existing_model_id
        else:
            model_id = normalize_model_id(target.stem)
            base_id = model_id
            index = 1
            while model_id in payload['models']:
                model_id = f'{base_id}-{index}'
                index += 1

        existing = dict(existing_raw or {})
        existing.update({
            'id': model_id,
            'label': str(metadata.get('label') or existing.get('label') or target.stem),
            'uploaded': True,
            'updated_at': time.time(),
            'class_names_text': metadata.get('class_names_text') or existing.get('class_names_text') or '',
            'aim_class': metadata.get('aim_class', existing.get('aim_class', 0)),
            'aim_keys_text': metadata.get('aim_keys_text') or existing.get('aim_keys_text') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT',
            'target_hotkeys': metadata.get('target_hotkeys') or existing.get('target_hotkeys') or {},
            'runtime_overrides': metadata.get('runtime_overrides') or existing.get('runtime_overrides') or {},
            'notes': metadata.get('notes') or existing.get('notes') or '',
        })
        entry = self._entry_from_file(target, existing=existing, uploaded=True, check_runtime=True)
        payload['models'][model_id] = entry
        if not payload.get('selected_model_id'):
            payload['selected_model_id'] = model_id
            self.write_last_model_id(model_id)
        self.save(payload)
        config = self.ensure_model_config(entry)
        config.update(self._config_updates_from_model_entry(entry))
        self.save_model_config(model_id, config)
        return entry

    def update_model(self, model_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            payload = self.refresh(check_runtime=False)
        raw = payload.get('models', {}).get(model_id)
        if not isinstance(raw, dict):
            raise KeyError(f'model not found: {model_id}')
        updated = dict(raw)
        if 'label' in updates:
            updated['label'] = str(updates.get('label') or '').strip() or updated.get('label') or model_id
        if 'class_names_text' in updates or 'class_names' in updates:
            class_names = split_class_names(updates.get('class_names_text', updates.get('class_names', '')))
            updated['class_names'] = class_names
            updated['class_names_text'] = class_names_to_text(class_names)
        if 'aim_class' in updates:
            updated['aim_class'] = max(0, int(float(updates.get('aim_class', 0) or 0)))
        if 'aim_keys_text' in updates:
            updated['aim_keys_text'] = normalize_key_text(updates.get('aim_keys_text') or '') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT'
        if 'target_hotkeys' in updates:
            updated['target_hotkeys'] = normalize_target_hotkeys(updates.get('target_hotkeys'))
        if 'runtime_overrides' in updates:
            updated['runtime_overrides'] = normalize_runtime_overrides(updates.get('runtime_overrides'))
        if 'notes' in updates:
            updated['notes'] = str(updates.get('notes') or '')
        updated['updated_at'] = time.time()
        entry = self._entry_from_file(Path(str(updated.get('path'))), existing=updated, uploaded=bool(updated.get('uploaded')), check_runtime=True)
        payload['models'][model_id] = entry
        self.save(payload)
        config = self.ensure_model_config(entry)
        config.update(self._config_updates_from_model_entry(entry))
        self.save_model_config(model_id, config)
        return self._apply_config_profile(model_id, entry)

    def select_model(self, model_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self.refresh(check_runtime=False)
        raw = payload.get('models', {}).get(model_id)
        if not isinstance(raw, dict):
            raise KeyError(f'model not found: {model_id}')
        payload['selected_model_id'] = model_id
        self.write_last_model_id(model_id)
        self.save(payload)
        model = self.get_model(model_id, check_runtime=True)
        self.ensure_model_config(model)
        return model

    def delete_model(self, model_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self.refresh(check_runtime=False)
        raw = payload.get('models', {}).get(model_id)
        if not isinstance(raw, dict):
            raise KeyError(f'model not found: {model_id}')
        model_path = Path(str(raw.get('path') or ''))
        if not self._is_model_in_managed_dir(model_path):
            raise ValueError('only models inside the managed models directory can be deleted from the web console')
        if model_path.exists():
            model_path.unlink()
        config_path = self.model_config_path(model_id)
        if config_path.exists():
            config_path.unlink()
        del payload['models'][model_id]
        if payload.get('selected_model_id') == model_id:
            payload['selected_model_id'] = ''
        self.save(payload)
        return self.list_payload(check_runtime=False)

    def selected_model_path(self) -> Path | None:
        payload = self.refresh(check_runtime=False)
        model_id = str(payload.get('selected_model_id') or '')
        raw = payload.get('models', {}).get(model_id)
        if not isinstance(raw, dict):
            return None
        path = Path(str(raw.get('path') or ''))
        return path if path.exists() else None


if __name__ == '__main__':
    store = ModelStore()
    if len(sys.argv) > 1 and sys.argv[1] == 'selected-path':
        path = store.selected_model_path()
        if path is not None:
            print(path)
        raise SystemExit(0)
    if len(sys.argv) > 1 and sys.argv[1] == 'selected-config':
        path = store.selected_model_config_path()
        if path is not None:
            print(path)
        raise SystemExit(0)
    if len(sys.argv) > 1 and sys.argv[1] == 'selected-id':
        payload = store.refresh(check_runtime=False)
        selected_id = str(payload.get('selected_model_id') or '')
        if selected_id:
            store.write_last_model_id(selected_id)
            print(selected_id)
        raise SystemExit(0)
    print(json.dumps(store.list_payload(check_runtime=False), ensure_ascii=False, indent=2))
