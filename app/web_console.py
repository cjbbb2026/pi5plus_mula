#!/usr/bin/env python3
from __future__ import annotations

import argparse
try:
    import cgi  # type: ignore
except Exception:
    cgi = None  # type: ignore
import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from control_client import PassthroughController
from hdmirx_edid import DEFAULT_PROFILE_NAME as DEFAULT_EDID_PROFILE_NAME
from hdmirx_edid import EDID_PROFILE_MAP, current_active_profile as current_edid_profile, load_identity as load_edid_identity, profile_payload, random_identity as random_edid_identity
from model_manager import (
    DEFAULT_CONFIG_DIR as MODEL_CONFIG_DIR_DEFAULT,
    DEFAULT_LAST_MODEL_FILE as LAST_MODEL_FILE_DEFAULT,
    DEFAULT_MODEL_DIR as MODEL_DIR_DEFAULT,
    DEFAULT_MODEL_STORE as MODEL_STORE_DEFAULT,
    ModelStore,
    normalize_key_text,
    normalize_target_hotkeys,
)
from runtime_config import RUNTIME_SCHEMA, runtime_defaults, sanitize_runtime_patch, sanitize_runtime_updates

ROOT = Path(__file__).resolve().parent.parent
HTML_FILE = Path(__file__).with_name('web_console.html')
DEFAULT_STATE_FILE = ROOT / '.ai_state.json'
DEFAULT_AI_LOG = ROOT / '.ai.log'
DEFAULT_PASSTHROUGH_LOG = ROOT / '.passthrough.log'
DEFAULT_AI_PID = ROOT / '.ai.pid'
DEFAULT_PREVIEW_FILE = ROOT / '.ai_preview.jpg'
DEFAULT_PRESETS_FILE = ROOT / '.ai_presets.json'
DEFAULT_MODEL_STORE = MODEL_STORE_DEFAULT
DEFAULT_MODEL_DIR = MODEL_DIR_DEFAULT
DEFAULT_AI_CMD_FILE = ROOT / '.ai_command.json'
DEFAULT_GUI_ENV_FILE = ROOT / '.gui_env'
DEFAULT_KM_SCRIPT = ROOT / 'scripts' / 'km_passthrough.sh'
DEFAULT_HDMIRX_EDID_SCRIPT = ROOT / 'app' / 'hdmirx_edid.py'
BUILTIN_PRESETS = {
    'balanced': {'label': '\u5747\u8861\u6a21\u5f0f', 'description': '\u6781\u7b80\u79fb\u52a8 + demo \u8D1D\u585E\u5C14\u8F68\u8FF9\u9ED8\u8BA4\u503C\u3002', 'values': {'conf': 0.25, 'nms': 0.45, 'aim_axis_x_scale': 1.0, 'aim_axis_y_scale': 1.0, 'control_max_step': 32, 'bezier_segment_dist_1': 3.0, 'bezier_segment_dist_2': 8.0, 'bezier_segment_dist_3': 20.0, 'bezier_curve_min_distance': 15.0, 'bezier_curve_scale': 0.03, 'bezier_jitter_min_distance': 10.0, 'bezier_jitter_scale': 0.01, 'bezier_jitter_damping': 0.3}},
    'fast_lock': {'label': '\u5feb\u901f\u79fb\u52a8', 'description': '\u63d0\u9AD8\u8F74\u500D\u7387\uFF0C\u540C\u65F6\u8BA9\u8D1D\u585E\u5C14\u8F68\u8FF9\u66F4\u76F4\u63A5\u3002', 'values': {'conf': 0.22, 'nms': 0.45, 'aim_axis_x_scale': 1.15, 'aim_axis_y_scale': 1.15, 'control_max_step': 48, 'bezier_segment_dist_1': 4.0, 'bezier_segment_dist_2': 10.0, 'bezier_segment_dist_3': 24.0, 'bezier_curve_min_distance': 18.0, 'bezier_curve_scale': 0.02, 'bezier_jitter_min_distance': 14.0, 'bezier_jitter_scale': 0.006, 'bezier_jitter_damping': 0.2}},
    'smooth_stable': {'label': '\u7A33\u5B9A\u7EC6\u8DDF', 'description': '\u964D\u4F4E\u8F74\u500D\u7387\u548C\u5355\u5E27\u6B65\u957F\u4E0A\u9650\uFF0C\u4FDD\u7559\u66F4\u67D4\u7684\u8F68\u8FF9\u3002', 'values': {'conf': 0.28, 'nms': 0.45, 'aim_axis_x_scale': 0.9, 'aim_axis_y_scale': 0.9, 'control_max_step': 24, 'bezier_segment_dist_1': 3.0, 'bezier_segment_dist_2': 7.0, 'bezier_segment_dist_3': 16.0, 'bezier_curve_min_distance': 12.0, 'bezier_curve_scale': 0.04, 'bezier_jitter_min_distance': 8.0, 'bezier_jitter_scale': 0.012, 'bezier_jitter_damping': 0.35}},
}


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


class ConsoleApp:
    def __init__(
        self,
        state_file: Path,
        ai_log: Path,
        passthrough_log: Path,
        ai_pid_file: Path,
        passthrough_socket: str,
        preview_file: Path,
        presets_file: Path,
        ai_cmd_file: Path,
        km_script: Path,
        hdmirx_edid_script: Path,
        model_store_file: Path,
        model_dir: Path,
        config_dir: Path,
        last_model_file: Path,
    ) -> None:
        self.state_file = state_file
        self.ai_log = ai_log
        self.passthrough_log = passthrough_log
        self.ai_pid_file = ai_pid_file
        self.passthrough_socket = passthrough_socket
        self.preview_file = preview_file
        self.presets_file = presets_file
        self.ai_cmd_file = ai_cmd_file
        self.km_script = km_script
        self.hdmirx_edid_script = hdmirx_edid_script
        self.model_store = ModelStore(model_store_file, model_dir, ROOT, config_dir, last_model_file)
        self._sudo_checked_at = 0.0
        self._sudo_available = False

    def read_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def tail_text(self, path: Path, lines: int) -> str:
        if not path.exists():
            return ''
        try:
            return '\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[-lines:])
        except Exception:
            return ''

    def read_pid(self, pid_file: Path) -> int | None:
        try:
            return int(pid_file.read_text(encoding='utf-8').strip())
        except Exception:
            return None

    def pid_running(self, pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False


    def process_matches_hint(self, pid: int, hint: str | None) -> bool:
        if not hint:
            return True
        try:
            cmdline = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\x00', b' ').decode('utf-8', errors='ignore')
        except Exception:
            return False
        return hint in cmdline

    def find_matching_pids(self, hint: str | None) -> list[int]:
        if not hint:
            return []
        try:
            completed = subprocess.run(['pgrep', '-f', hint], capture_output=True, text=True, timeout=3)
        except Exception:
            return []
        if completed.returncode not in (0, 1):
            return []
        pids: list[int] = []
        for raw in completed.stdout.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                pid = int(raw)
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            pids.append(pid)
        return pids

    def kill_pid_gracefully(self, pid: int, timeout: float = 3.0) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.pid_running(pid):
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def ai_running(self) -> bool:
        pid = self.read_pid(self.ai_pid_file)
        if self.pid_running(pid):
            return True
        return bool(self.find_matching_pids('app/ai.py'))

    def passthrough_state(self) -> dict:
        try:
            data = PassthroughController(self.passthrough_socket).get_state(timeout=0.2)
            if isinstance(data, dict):
                data['ok'] = True
                return data
            return {'ok': False, 'error': f'unexpected response: {data!r}'}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def sudo_available(self) -> bool:
        if getattr(os, 'geteuid', lambda: 1)() == 0:
            return True
        now = time.time()
        if now - self._sudo_checked_at < 10.0:
            return self._sudo_available
        self._sudo_checked_at = now
        try:
            completed = subprocess.run(['sudo', '-n', 'true'], capture_output=True, text=True, timeout=3)
            self._sudo_available = completed.returncode == 0
        except Exception:
            self._sudo_available = False
        return self._sudo_available

    def load_gui_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        env_file = DEFAULT_GUI_ENV_FILE
        if not env_file.exists():
            return env
        try:
            for line in env_file.read_text(encoding='utf-8', errors='ignore').splitlines():
                raw = line.strip()
                if not raw or '=' not in raw:
                    continue
                key, value = raw.split('=', 1)
                key = key.strip()
                if key in {'DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'} and value.strip():
                    env[key] = value.strip()
        except Exception:
            return {}
        return env

    def merged_config(self) -> dict:
        selected = self.current_selected_model()
        if not isinstance(selected, dict):
            return runtime_defaults()
        return self.model_store.ensure_model_config(selected)


    def parse_bool(self, value: object, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if not text:
            return default
        return text in {'1', 'true', 'yes', 'on'}

    def serialize_target_hotkeys(self, value: object) -> str:
        hotkeys = normalize_target_hotkeys(value)
        if not hotkeys:
            return ''
        return json.dumps(hotkeys, ensure_ascii=False, sort_keys=True)

    def parse_target_hotkeys(self, value: object) -> dict[str, str]:
        if isinstance(value, dict):
            return normalize_target_hotkeys(value)
        text = str(value or '').strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {}
            for line in text.splitlines():
                raw = line.strip()
                if not raw or ':' not in raw:
                    continue
                key, hotkey = raw.split(':', 1)
                parsed[key.strip()] = hotkey.strip()
        return normalize_target_hotkeys(parsed)

    def current_selected_model(self) -> dict | None:
        payload = self.model_store.list_payload(check_runtime=False)
        selected = payload.get('selected_model')
        return selected if isinstance(selected, dict) else None

    def current_selected_model_id(self) -> str:
        payload = self.model_store.list_payload(check_runtime=False)
        return str(payload.get('selected_model_id') or '').strip()

    def read_ai_cmd_meta(self) -> dict:
        meta = self.read_json(self.ai_cmd_file)
        cmd = meta.get('cmd')
        meta['cmd'] = cmd if isinstance(cmd, list) else []
        return meta

    def ai_show_enabled(self) -> bool:
        cmd = self.read_ai_cmd_meta().get('cmd') or []
        return '--show' in cmd

    def set_ai_show(self, enabled: bool, restart_ai: bool = True) -> dict:
        meta = self.read_ai_cmd_meta()
        cmd = list(meta.get('cmd') or [])
        had_show = '--show' in cmd
        if enabled and not had_show:
            cmd.append('--show')
        if not enabled and had_show:
            cmd = [item for item in cmd if item != '--show']
        meta['cmd'] = cmd
        meta['show_enabled'] = bool(enabled)
        Path(self.ai_cmd_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.ai_cmd_file).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        restart_result = None
        if restart_ai and self.ai_running():
            restart_result = self.restart_ai()
        return {'ok': True, 'show_enabled': bool(enabled), 'restart': restart_result, 'state': self.service_state()}

    def build_model_runtime_overrides(self, values: dict) -> dict:
        return sanitize_runtime_updates(values)

    def models_payload(self) -> dict:
        return {'ok': True, **self.model_store.list_payload(check_runtime=False)}

    def apply_model_defaults_to_config(self, model: dict) -> dict:
        return self.model_store.ensure_model_config(model)

    def sync_selected_model_to_config(self) -> dict | None:
        payload = self.model_store.list_payload(check_runtime=False)
        selected_id = str(payload.get('selected_model_id') or '').strip()
        if not selected_id:
            return None
        try:
            selected = self.model_store.get_model(selected_id, check_runtime=False)
        except Exception:
            selected = payload.get('selected_model')
        if not isinstance(selected, dict):
            return None
        return self.apply_model_defaults_to_config(selected)

    def sync_selected_model_to_ai_cmd(self) -> dict | None:
        payload = self.model_store.list_payload(check_runtime=False)
        selected_id = str(payload.get('selected_model_id') or '').strip()
        if not selected_id:
            return None
        try:
            selected = self.model_store.get_model(selected_id, check_runtime=True)
        except Exception:
            selected = payload.get('selected_model')
        if not isinstance(selected, dict):
            return None

        model_path = str(selected.get('path') or '').strip()
        if not model_path:
            return None

        meta = self.read_json(self.ai_cmd_file)
        cmd = meta.get('cmd')
        if not isinstance(cmd, list) or not cmd:
            return selected

        def upsert_arg(items: list[str], flag: str, value: str) -> list[str]:
            updated = list(items)
            if flag in updated:
                index = updated.index(flag)
                if index + 1 < len(updated):
                    updated[index + 1] = value
                else:
                    updated.append(value)
                return updated
            updated.extend([flag, value])
            return updated

        def remove_arg(items: list[str], flag: str) -> list[str]:
            updated: list[str] = []
            skip_next = False
            for index, item in enumerate(items):
                if skip_next:
                    skip_next = False
                    continue
                if item == flag:
                    if index + 1 < len(items):
                        skip_next = True
                    continue
                updated.append(item)
            return updated

        updated_cmd = upsert_arg(list(cmd), '--model', model_path)
        self.model_store.ensure_model_config(selected)
        config_path = str(self.model_store.model_config_path(str(selected.get('id') or '')))
        updated_cmd = upsert_arg(updated_cmd, '--runtime-config-file', config_path)

        input_width = selected.get('input_width')
        input_height = selected.get('input_height')
        input_source = str(selected.get('input_source') or '')
        trusted_input = bool(input_width and input_height and input_source in {'runtime', 'runtime-output-shape', 'output-shape-inferred'})
        if trusted_input:
            updated_cmd = upsert_arg(updated_cmd, '--model-input-width', str(int(input_width)))
            updated_cmd = upsert_arg(updated_cmd, '--model-input-height', str(int(input_height)))
        else:
            updated_cmd = remove_arg(updated_cmd, '--model-input-width')
            updated_cmd = remove_arg(updated_cmd, '--model-input-height')

        meta['cmd'] = updated_cmd
        meta['selected_model_id'] = selected.get('id')
        meta['selected_model_path'] = model_path
        meta['selected_model_config'] = config_path
        meta['selected_model_input_width'] = int(input_width) if input_width else 0
        meta['selected_model_input_height'] = int(input_height) if input_height else 0
        meta['selected_model_input_source'] = input_source
        Path(self.ai_cmd_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.ai_cmd_file).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        return selected

    def save_model_profile(self, model_id: str, values: dict) -> dict:
        updates = dict(values)
        if 'target_hotkeys' in updates:
            updates['target_hotkeys'] = normalize_target_hotkeys(updates.get('target_hotkeys'))
        model = self.model_store.update_model(model_id, updates)
        selected_id = self.model_store.list_payload(check_runtime=False).get('selected_model_id')
        config = None
        if model_id == selected_id:
            config = self.model_store.ensure_model_config(model)
            config_updates: dict[str, object] = {}
            if 'aim_class' in updates:
                config_updates['aim_class'] = int(model.get('aim_class', 0) or 0)
            if 'aim_keys_text' in updates:
                config_updates['aim_keys_text'] = normalize_key_text(model.get('aim_keys_text') or '') or 'KEY_LEFTSHIFT,KEY_RIGHTSHIFT'
            if 'target_hotkeys' in updates:
                config_updates['aim_target_keys_text'] = self.serialize_target_hotkeys(model.get('target_hotkeys'))
            if config_updates:
                config = self.model_store.save_model_config(model_id, sanitize_runtime_patch(config, config_updates))
            self.sync_selected_model_to_ai_cmd()
        return {'ok': True, 'model': model, 'config': config, **self.model_store.list_payload(check_runtime=False)}

    def upload_model_bytes(self, filename: str, content: bytes, metadata: dict) -> dict:
        prepared = dict(metadata)
        prepared['target_hotkeys'] = normalize_target_hotkeys(metadata.get('target_hotkeys'))
        model = self.model_store.register_uploaded_model(filename, content, prepared)
        if self.parse_bool(metadata.get('select_after_upload'), False):
            self.model_store.select_model(str(model.get('id')))
            model = self.model_store.get_model(str(model.get('id')), check_runtime=True)
            self.apply_model_defaults_to_config(model)
            self.sync_selected_model_to_ai_cmd()
            if self.ai_running():
                self.restart_ai()
        return {'ok': True, 'model': model, **self.model_store.list_payload(check_runtime=False)}

    def select_model(self, model_id: str, restart_ai: bool = True) -> dict:
        model = self.model_store.select_model(model_id)
        config = self.apply_model_defaults_to_config(model)
        self.sync_selected_model_to_ai_cmd()
        restart_result = None
        if restart_ai and self.ai_running():
            restart_result = self.restart_ai()
        return {'ok': True, 'model': model, 'config': config, 'restart': restart_result, **self.model_store.list_payload(check_runtime=False)}

    def delete_model(self, model_id: str) -> dict:
        payload = self.model_store.delete_model(model_id)
        self.sync_selected_model_to_ai_cmd()
        return {'ok': True, **payload}

    def service_state(self) -> dict:
        ai_pid = self.read_pid(self.ai_pid_file)
        passthrough = self.passthrough_state()
        return {
            'ai': {'running': self.ai_running(), 'pid': ai_pid, 'show_enabled': self.ai_show_enabled()},
            'web': {'pid': os.getpid()},
            'passthrough': {
                'socket_ok': bool(passthrough.get('ok')),
                'socket_path': self.passthrough_socket,
                'sudo_available': self.sudo_available(),
            },
            'edid': {
                'script_exists': self.hdmirx_edid_script.exists(),
                'profiles': profile_payload(visible_only=True),
                'identity': load_edid_identity(),
                'active_profile': current_edid_profile(),
            },
        }

    def build_state(self) -> dict:
        preview_exists = self.preview_file.exists()
        preview_stat = self.preview_file.stat() if preview_exists else None
        models = self.model_store.list_payload(check_runtime=False)
        selected_config = self.model_store.selected_model_config_path()
        return {
            'ok': True,
            'ai_running': self.ai_running(),
            'ai_state': self.read_json(self.state_file),
            'config': self.merged_config(),
            'config_path': str(selected_config) if selected_config is not None else '',
            'passthrough': self.passthrough_state(),
            'passthrough_socket': self.passthrough_socket,
            'preview_exists': preview_exists,
            'preview_file': str(self.preview_file),
            'preview_mtime': preview_stat.st_mtime if preview_stat else None,
            'preview_size': preview_stat.st_size if preview_stat else 0,
            'services': self.service_state(),
            'ai_show_enabled': self.ai_show_enabled(),
            'models': models,
        }

    def save_config(self, values: dict) -> dict:
        model_id = self.current_selected_model_id()
        if not model_id:
            raise RuntimeError('no selected model')
        merged = sanitize_runtime_patch(self.merged_config(), values)
        return self.model_store.save_model_config(model_id, merged)

    def reset_config(self) -> dict:
        selected = self.current_selected_model()
        if not isinstance(selected, dict):
            return runtime_defaults()
        defaults = self.model_store.default_runtime_config_for_model(selected)
        return self.model_store.save_model_config(str(selected.get('id') or ''), defaults)

    def preview_bytes(self) -> bytes | None:
        try:
            return self.preview_file.read_bytes()
        except Exception:
            return None

    def list_presets(self) -> list[dict]:
        items = []
        for name, preset in BUILTIN_PRESETS.items():
            items.append({'name': name, 'source': 'builtin', **preset})
        custom = self.read_json(self.presets_file).get('presets', {})
        if isinstance(custom, dict):
            for name in sorted(custom):
                preset = custom.get(name)
                if isinstance(preset, dict) and isinstance(preset.get('values'), dict):
                    items.append({'name': name, 'source': 'custom', 'label': preset.get('label', name), 'description': preset.get('description', ''), 'values': sanitize_runtime_updates(preset['values'])})
        return items

    def save_preset(self, name: str, values: dict) -> dict:
        name = name.strip()
        if not name:
            raise ValueError('\u9884\u8BBE\u540D\u79F0\u4E0D\u80FD\u4E3A\u7A7A')
        clean = sanitize_runtime_updates(values)
        if not clean:
            raise ValueError('\u9884\u8BBE\u53C2\u6570\u4E0D\u80FD\u4E3A\u7A7A')
        payload = self.read_json(self.presets_file)
        presets = payload.get('presets', {}) if isinstance(payload.get('presets'), dict) else {}
        presets[name] = {'label': name, 'description': '\u4ECE\u7F51\u9875\u63A7\u5236\u53F0\u4FDD\u5B58\u3002', 'updated_at': time.time(), 'values': clean}
        atomic_write_json(self.presets_file, {'presets': presets})
        return presets[name]

    def apply_preset(self, name: str) -> dict:
        for preset in self.list_presets():
            if preset['name'] == name:
                merged = sanitize_runtime_patch(self.merged_config(), preset['values'])
                model_id = self.current_selected_model_id()
                if not model_id:
                    raise RuntimeError('no selected model')
                return self.model_store.save_model_config(model_id, merged)
        raise KeyError(f"'\u672A\u627E\u5230\u9884\u8BBE: {name}'")

    def delete_preset(self, name: str) -> None:
        payload = self.read_json(self.presets_file)
        presets = payload.get('presets', {}) if isinstance(payload.get('presets'), dict) else {}
        if name not in presets:
            raise KeyError(f"'\u672A\u627E\u5230\u81EA\u5B9A\u4E49\u9884\u8BBE: {name}'")
        del presets[name]
        atomic_write_json(self.presets_file, {'presets': presets})

    def control(self, body: dict) -> dict:
        action = str(body.get('action', '')).strip().lower()
        ctl = PassthroughController(self.passthrough_socket)
        if action in {'key_tap', 'key_down', 'key_up'}:
            key = str(body.get('key', '')).strip()
            if not key:
                raise ValueError('key is required')
            getattr(ctl, action)(key)
        elif action == 'mouse_move':
            ctl.mouse_move(int(body.get('dx', 0)), int(body.get('dy', 0)))
        elif action in {'mouse_click', 'mouse_down', 'mouse_up'}:
            button = str(body.get('button', 'left')).strip().lower() or 'left'
            getattr(ctl, action)(button)
        elif action == 'mouse_scroll':
            ctl.mouse_scroll(int(body.get('wheel', 0)))
        elif action == 'release_all':
            ctl.release_all()
        else:
            raise ValueError(f'unsupported action: {action}')
        return {'ok': True, 'action': action, 'passthrough': self.passthrough_state()}

    def stop_pid_file(self, pid_file: Path, timeout: float = 3.0, hint: str | None = None) -> dict:
        pid = self.read_pid(pid_file)
        changed = False
        if pid and self.pid_running(pid) and self.process_matches_hint(pid, hint):
            self.kill_pid_gracefully(pid, timeout=timeout)
            changed = True
        matching_pids = self.find_matching_pids(hint)
        for extra_pid in matching_pids:
            if pid is not None and extra_pid == pid:
                continue
            self.kill_pid_gracefully(extra_pid, timeout=timeout)
            changed = True
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        return {'changed': changed, 'pid': pid}

    def start_ai(self) -> dict:
        if self.ai_running():
            return {'ok': True, 'changed': False, 'service': 'ai', 'state': self.service_state()}
        self.sync_selected_model_to_config()
        self.sync_selected_model_to_ai_cmd()
        meta = self.read_json(self.ai_cmd_file)
        cmd = meta.get('cmd')
        if not isinstance(cmd, list) or not cmd:
            raise RuntimeError(f'AI command file missing or invalid: {self.ai_cmd_file}')
        project_dir = str(meta.get('project_dir') or ROOT)
        home = str(meta.get('home') or os.environ.get('HOME', ''))
        log_file = Path(str(meta.get('log_file') or self.ai_log)).expanduser()
        pid_file = Path(str(meta.get('pid_file') or self.ai_pid_file)).expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        if home:
            env['HOME'] = home
        gui_env = self.load_gui_env()
        for key, value in gui_env.items():
            env[key] = value
        if 'XAUTHORITY' not in env and home:
            fallback_xauth = str(Path(home) / '.Xauthority')
            if Path(fallback_xauth).exists():
                env['XAUTHORITY'] = fallback_xauth
        with log_file.open('ab') as fp:
            proc = subprocess.Popen(cmd, cwd=project_dir, stdout=fp, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        pid_file.write_text(str(proc.pid), encoding='utf-8')
        return {'ok': True, 'changed': True, 'service': 'ai', 'pid': proc.pid, 'state': self.service_state()}

    def stop_ai(self) -> dict:
        result = self.stop_pid_file(self.ai_pid_file, hint='app/ai.py')
        return {'ok': True, 'service': 'ai', **result, 'state': self.service_state()}

    def restart_ai(self) -> dict:
        self.sync_selected_model_to_ai_cmd()
        self.stop_pid_file(self.ai_pid_file, hint='app/ai.py')
        return self.start_ai()

    def run_root_script(self, script: Path, args: list[str], timeout: int = 30) -> dict:
        if not script.exists():
            raise RuntimeError(f'script not found: {script}')
        command = ['bash', str(script)] + list(args)
        if getattr(os, 'geteuid', lambda: 1)() != 0:
            command = ['sudo', '-n'] + command
        completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or f'command failed: {completed.returncode}').strip()
            raise RuntimeError(message)
        return {'output': (completed.stdout or '').strip()}

    def run_passthrough_script(self, action: str) -> dict:
        return self.run_root_script(self.km_script, [action], timeout=30)

    def run_edid_tool(self, args: list[str], timeout: int = 40) -> dict:
        if not self.hdmirx_edid_script.exists():
            raise RuntimeError(f'edid tool not found: {self.hdmirx_edid_script}')
        command = [sys.executable, str(self.hdmirx_edid_script)] + list(args)
        if getattr(os, 'geteuid', lambda: 1)() != 0:
            command = ['sudo', '-n'] + command
        completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or f'command failed: {completed.returncode}').strip()
            raise RuntimeError(message)
        return {'output': (completed.stdout or '').strip()}

    def apply_edid_profile(self, name: str) -> dict:
        profile = EDID_PROFILE_MAP.get(name or DEFAULT_EDID_PROFILE_NAME)
        if profile is None:
            raise ValueError(f'unsupported edid profile: {name}')
        result = self.run_edid_tool(['apply', profile.name], timeout=40)
        return {'ok': True, 'profile': profile.name, 'identity': load_edid_identity(), **profile.to_dict(), **result, 'state': self.service_state()}

    def randomize_edid_identity(self, apply_current: bool = True) -> dict:
        identity = random_edid_identity()
        result: dict = {}
        active_profile = current_edid_profile()
        if apply_current:
            result = self.run_edid_tool(['apply', active_profile], timeout=40)
        return {'ok': True, 'identity': identity, 'profile': active_profile, **result, 'state': self.service_state()}

    def service_action(self, service: str, action: str) -> dict:
        service = service.strip().lower()
        action = action.strip().lower()
        if service == 'ai':
            if action == 'start':
                return self.start_ai()
            if action == 'stop':
                return self.stop_ai()
            if action == 'restart':
                return self.restart_ai()
            if action == 'status':
                return {'ok': True, 'service': 'ai', 'state': self.service_state()}
        if service == 'passthrough':
            mapped = {'start': 'start-auto-mouse', 'stop': 'stop', 'restart': 'restart', 'status': 'status'}.get(action)
            if not mapped:
                raise ValueError(f'unsupported passthrough action: {action}')
            result = self.run_passthrough_script(mapped)
            return {'ok': True, 'service': 'passthrough', 'action': action, **result, 'state': self.service_state()}
        raise ValueError(f'unsupported service: {service}')


class Handler(BaseHTTPRequestHandler):
    @property
    def app(self) -> ConsoleApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        return

    def read_body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0') or '0')) or b'{}'
        data = json.loads(raw.decode('utf-8'))
        return data if isinstance(data, dict) else {}

    def read_multipart_form(self) -> dict:
        if cgi is None:
            raise RuntimeError('multipart upload is unavailable in this Python environment')
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                'CONTENT_LENGTH': self.headers.get('Content-Length', '0'),
            },
            keep_blank_values=True,
        )
        payload: dict[str, object] = {}
        for key in form.keys():
            field = form[key]
            if isinstance(field, list):
                field = field[0]
            if getattr(field, 'filename', None):
                payload[key] = {
                    'filename': str(field.filename),
                    'content': field.file.read(),
                }
            else:
                payload[key] = field.value
        return payload

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == '/':
            self.send_bytes(HTML_FILE.read_bytes(), 'text/html; charset=utf-8')
            return
        if parsed.path == '/api/schema':
            self.send_json({'ok': True, 'schema': RUNTIME_SCHEMA})
            return
        if parsed.path == '/api/state':
            self.send_json(self.app.build_state())
            return
        if parsed.path == '/api/presets':
            self.send_json({'ok': True, 'presets': self.app.list_presets()})
            return
        if parsed.path == '/api/models':
            self.send_json(self.app.models_payload())
            return
        if parsed.path == '/api/logs':
            params = parse_qs(parsed.query)
            name = params.get('name', ['ai'])[0]
            lines = max(10, min(500, int(params.get('lines', ['80'])[0])))
            path = self.app.ai_log if name == 'ai' else self.app.passthrough_log
            self.send_json({'ok': True, 'text': self.app.tail_text(path, lines)})
            return
        if parsed.path == '/api/preview.jpg':
            data = self.app.preview_bytes()
            if data is None:
                self.send_json({'ok': False, 'error': 'preview not ready'}, 404)
            else:
                self.send_bytes(data, 'image/jpeg')
            return
        self.send_json({'ok': False, 'error': 'not found'}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == '/api/models/upload':
                form = self.read_multipart_form()
                upload = form.get('file')
                if not isinstance(upload, dict):
                    raise ValueError('missing file')
                filename = str(upload.get('filename') or '').strip()
                content = upload.get('content')
                if not filename or not isinstance(content, (bytes, bytearray)):
                    raise ValueError('invalid file payload')
                metadata = {
                    'label': form.get('label', ''),
                    'class_names_text': form.get('class_names_text', ''),
                    'aim_class': form.get('aim_class', 0),
                    'aim_keys_text': form.get('aim_keys_text', ''),
                    'notes': form.get('notes', ''),
                    'select_after_upload': form.get('select_after_upload', '0'),
                }
                self.send_json(self.app.upload_model_bytes(filename, bytes(content), metadata))
                return

            body = self.read_body()
            if parsed.path == '/api/config':
                values = body.get('values', body)
                if not isinstance(values, dict):
                    raise ValueError('values must be an object')
                self.send_json({'ok': True, 'config': self.app.save_config(values)})
                return
            if parsed.path == '/api/config/reset':
                self.send_json({'ok': True, 'config': self.app.reset_config()})
                return
            if parsed.path == '/api/control':
                self.send_json(self.app.control(body))
                return
            if parsed.path == '/api/service':
                self.send_json(self.app.service_action(str(body.get('service', '')), str(body.get('action', ''))))
                return
            if parsed.path == '/api/ai/show':
                enabled = self.app.parse_bool(body.get('enabled'), False)
                restart_ai = self.app.parse_bool(body.get('restart_ai'), True)
                self.send_json(self.app.set_ai_show(enabled, restart_ai=restart_ai))
                return
            if parsed.path == '/api/presets/save':
                values = body.get('values', body.get('config'))
                if not isinstance(values, dict):
                    raise ValueError('values must be an object')
                self.send_json({'ok': True, 'preset': self.app.save_preset(str(body.get('name', '')), values)})
                return
            if parsed.path == '/api/presets/apply':
                self.send_json({'ok': True, 'config': self.app.apply_preset(str(body.get('name', '')))})
                return
            if parsed.path == '/api/presets/delete':
                self.app.delete_preset(str(body.get('name', '')))
                self.send_json({'ok': True})
                return
            if parsed.path == '/api/edid/apply':
                self.send_json(self.app.apply_edid_profile(str(body.get('name', DEFAULT_EDID_PROFILE_NAME)).strip()))
                return
            if parsed.path == '/api/edid/identity/random':
                self.send_json(self.app.randomize_edid_identity(apply_current=self.app.parse_bool(body.get('apply_current'), True)))
                return
            if parsed.path == '/api/models/save':
                values = body.get('values', body)
                if not isinstance(values, dict):
                    raise ValueError('values must be an object')
                model_id = str(body.get('id') or values.get('id') or '').strip()
                if not model_id:
                    raise ValueError('model id is required')
                self.send_json(self.app.save_model_profile(model_id, values))
                return
            if parsed.path == '/api/models/select':
                model_id = str(body.get('id', '')).strip()
                if not model_id:
                    raise ValueError('model id is required')
                restart_ai = self.app.parse_bool(body.get('restart_ai'), True)
                self.send_json(self.app.select_model(model_id, restart_ai=restart_ai))
                return
            if parsed.path == '/api/models/delete':
                model_id = str(body.get('id', '')).strip()
                if not model_id:
                    raise ValueError('model id is required')
                self.send_json(self.app.delete_model(model_id))
                return
        except Exception as exc:
            self.send_json({'ok': False, 'error': str(exc)}, 400)
            return
        self.send_json({'ok': False, 'error': 'not found'}, 404)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='OrangePi web console')
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8080)
    p.add_argument('--state-file', default=str(DEFAULT_STATE_FILE))
    p.add_argument('--ai-log', default=str(DEFAULT_AI_LOG))
    p.add_argument('--passthrough-log', default=str(DEFAULT_PASSTHROUGH_LOG))
    p.add_argument('--ai-pid-file', default=str(DEFAULT_AI_PID))
    p.add_argument('--passthrough-socket', default='/tmp/km_passthrough.sock')
    p.add_argument('--preview-file', default=str(DEFAULT_PREVIEW_FILE))
    p.add_argument('--presets-file', default=str(DEFAULT_PRESETS_FILE))
    p.add_argument('--ai-cmd-file', default=str(DEFAULT_AI_CMD_FILE))
    p.add_argument('--km-script', default=str(DEFAULT_KM_SCRIPT))
    p.add_argument('--hdmirx-edid-script', default=str(DEFAULT_HDMIRX_EDID_SCRIPT))
    p.add_argument('--model-store-file', default=str(DEFAULT_MODEL_STORE))
    p.add_argument('--model-dir', default=str(DEFAULT_MODEL_DIR))
    p.add_argument('--config-dir', default=str(MODEL_CONFIG_DIR_DEFAULT))
    p.add_argument('--last-model-file', default=str(LAST_MODEL_FILE_DEFAULT))
    p.add_argument('--config-file', default='', help='deprecated compatibility option')
    p.add_argument('--persist-config-file', default='', help='deprecated compatibility option')
    return p.parse_args()


def main() -> int:
    a = parse_args()
    app = ConsoleApp(
        Path(a.state_file).expanduser().resolve(),
        Path(a.ai_log).expanduser().resolve(),
        Path(a.passthrough_log).expanduser().resolve(),
        Path(a.ai_pid_file).expanduser().resolve(),
        a.passthrough_socket,
        Path(a.preview_file).expanduser().resolve(),
        Path(a.presets_file).expanduser().resolve(),
        Path(a.ai_cmd_file).expanduser().resolve(),
        Path(a.km_script).expanduser().resolve(),
        Path(a.hdmirx_edid_script).expanduser().resolve(),
        Path(a.model_store_file).expanduser().resolve(),
        Path(a.model_dir).expanduser().resolve(),
        Path(a.config_dir).expanduser().resolve(),
        Path(a.last_model_file).expanduser().resolve(),
    )
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    server.app = app  # type: ignore[attr-defined]
    print(f'Web console: http://{a.host}:{a.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
