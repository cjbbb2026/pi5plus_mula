#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from evdev import InputDevice, ecodes


MODIFIER_MAP = {
    ecodes.KEY_LEFTCTRL: 0x01,
    ecodes.KEY_LEFTSHIFT: 0x02,
    ecodes.KEY_LEFTALT: 0x04,
    ecodes.KEY_LEFTMETA: 0x08,
    ecodes.KEY_RIGHTCTRL: 0x10,
    ecodes.KEY_RIGHTSHIFT: 0x20,
    ecodes.KEY_RIGHTALT: 0x40,
    ecodes.KEY_RIGHTMETA: 0x80,
}

KEY_MAP = {
    ecodes.KEY_A: 0x04,
    ecodes.KEY_B: 0x05,
    ecodes.KEY_C: 0x06,
    ecodes.KEY_D: 0x07,
    ecodes.KEY_E: 0x08,
    ecodes.KEY_F: 0x09,
    ecodes.KEY_G: 0x0A,
    ecodes.KEY_H: 0x0B,
    ecodes.KEY_I: 0x0C,
    ecodes.KEY_J: 0x0D,
    ecodes.KEY_K: 0x0E,
    ecodes.KEY_L: 0x0F,
    ecodes.KEY_M: 0x10,
    ecodes.KEY_N: 0x11,
    ecodes.KEY_O: 0x12,
    ecodes.KEY_P: 0x13,
    ecodes.KEY_Q: 0x14,
    ecodes.KEY_R: 0x15,
    ecodes.KEY_S: 0x16,
    ecodes.KEY_T: 0x17,
    ecodes.KEY_U: 0x18,
    ecodes.KEY_V: 0x19,
    ecodes.KEY_W: 0x1A,
    ecodes.KEY_X: 0x1B,
    ecodes.KEY_Y: 0x1C,
    ecodes.KEY_Z: 0x1D,
    ecodes.KEY_1: 0x1E,
    ecodes.KEY_2: 0x1F,
    ecodes.KEY_3: 0x20,
    ecodes.KEY_4: 0x21,
    ecodes.KEY_5: 0x22,
    ecodes.KEY_6: 0x23,
    ecodes.KEY_7: 0x24,
    ecodes.KEY_8: 0x25,
    ecodes.KEY_9: 0x26,
    ecodes.KEY_0: 0x27,
    ecodes.KEY_ENTER: 0x28,
    ecodes.KEY_ESC: 0x29,
    ecodes.KEY_BACKSPACE: 0x2A,
    ecodes.KEY_TAB: 0x2B,
    ecodes.KEY_SPACE: 0x2C,
    ecodes.KEY_MINUS: 0x2D,
    ecodes.KEY_EQUAL: 0x2E,
    ecodes.KEY_LEFTBRACE: 0x2F,
    ecodes.KEY_RIGHTBRACE: 0x30,
    ecodes.KEY_BACKSLASH: 0x31,
    ecodes.KEY_SEMICOLON: 0x33,
    ecodes.KEY_APOSTROPHE: 0x34,
    ecodes.KEY_GRAVE: 0x35,
    ecodes.KEY_COMMA: 0x36,
    ecodes.KEY_DOT: 0x37,
    ecodes.KEY_SLASH: 0x38,
    ecodes.KEY_CAPSLOCK: 0x39,
    ecodes.KEY_F1: 0x3A,
    ecodes.KEY_F2: 0x3B,
    ecodes.KEY_F3: 0x3C,
    ecodes.KEY_F4: 0x3D,
    ecodes.KEY_F5: 0x3E,
    ecodes.KEY_F6: 0x3F,
    ecodes.KEY_F7: 0x40,
    ecodes.KEY_F8: 0x41,
    ecodes.KEY_F9: 0x42,
    ecodes.KEY_F10: 0x43,
    ecodes.KEY_F11: 0x44,
    ecodes.KEY_F12: 0x45,
    ecodes.KEY_SYSRQ: 0x46,
    ecodes.KEY_SCROLLLOCK: 0x47,
    ecodes.KEY_PAUSE: 0x48,
    ecodes.KEY_INSERT: 0x49,
    ecodes.KEY_HOME: 0x4A,
    ecodes.KEY_PAGEUP: 0x4B,
    ecodes.KEY_DELETE: 0x4C,
    ecodes.KEY_END: 0x4D,
    ecodes.KEY_PAGEDOWN: 0x4E,
    ecodes.KEY_RIGHT: 0x4F,
    ecodes.KEY_LEFT: 0x50,
    ecodes.KEY_DOWN: 0x51,
    ecodes.KEY_UP: 0x52,
    ecodes.KEY_NUMLOCK: 0x53,
    ecodes.KEY_KPSLASH: 0x54,
    ecodes.KEY_KPASTERISK: 0x55,
    ecodes.KEY_KPMINUS: 0x56,
    ecodes.KEY_KPPLUS: 0x57,
    ecodes.KEY_KPENTER: 0x58,
    ecodes.KEY_KP1: 0x59,
    ecodes.KEY_KP2: 0x5A,
    ecodes.KEY_KP3: 0x5B,
    ecodes.KEY_KP4: 0x5C,
    ecodes.KEY_KP5: 0x5D,
    ecodes.KEY_KP6: 0x5E,
    ecodes.KEY_KP7: 0x5F,
    ecodes.KEY_KP8: 0x60,
    ecodes.KEY_KP9: 0x61,
    ecodes.KEY_KP0: 0x62,
    ecodes.KEY_KPDOT: 0x63,
    ecodes.KEY_102ND: 0x64,
    ecodes.KEY_COMPOSE: 0x65,
    ecodes.KEY_POWER: 0x66,
    ecodes.KEY_KPEQUAL: 0x67,
    ecodes.KEY_F13: 0x68,
    ecodes.KEY_F14: 0x69,
    ecodes.KEY_F15: 0x6A,
    ecodes.KEY_F16: 0x6B,
    ecodes.KEY_F17: 0x6C,
    ecodes.KEY_F18: 0x6D,
    ecodes.KEY_F19: 0x6E,
    ecodes.KEY_F20: 0x6F,
    ecodes.KEY_F21: 0x70,
    ecodes.KEY_F22: 0x71,
    ecodes.KEY_F23: 0x72,
    ecodes.KEY_F24: 0x73,
}

MOUSE_BUTTON_MAP = {
    ecodes.BTN_LEFT: 0x01,
    ecodes.BTN_RIGHT: 0x02,
    ecodes.BTN_MIDDLE: 0x04,
}

BUTTON_NAME_MAP = {
    "left": ecodes.BTN_LEFT,
    "right": ecodes.BTN_RIGHT,
    "middle": ecodes.BTN_MIDDLE,
    "btn_left": ecodes.BTN_LEFT,
    "btn_right": ecodes.BTN_RIGHT,
    "btn_middle": ecodes.BTN_MIDDLE,
}

LISTEN_KEYBOARD_KEYS = {
    ecodes.KEY_A,
    ecodes.KEY_Z,
    ecodes.KEY_ENTER,
    ecodes.KEY_SPACE,
}


COMMAND_HELP = {
    "key": {"type": "key", "key": "KEY_A", "action": "down|up|tap"},
    "mouse_move": {"type": "mouse_move", "dx": 10, "dy": -5},
    "mouse_button": {"type": "mouse_button", "button": "left", "action": "down|up|click"},
    "mouse_scroll": {"type": "mouse_scroll", "wheel": 1},
    "release_all": {"type": "release_all"},
    "key_state": {"type": "key_state", "key": "KEY_A"},
    "mouse_button_state": {"type": "mouse_button_state", "button": "left"},
    "get_state": {"type": "get_state"},
}


def clamp_rel(value: int) -> int:
    return max(-127, min(127, value))


def signed_byte(value: int) -> int:
    return value & 0xFF


def describe_capabilities(device: InputDevice) -> str:
    caps = device.capabilities()
    key_count = len(caps.get(ecodes.EV_KEY, []))
    rel_codes = caps.get(ecodes.EV_REL, [])
    return f"name={device.name!r} path={device.path} keys={key_count} rel={rel_codes}"


def is_keyboard(device: InputDevice) -> bool:
    caps = device.capabilities()
    keys = set(caps.get(ecodes.EV_KEY, []))
    return LISTEN_KEYBOARD_KEYS.issubset(keys)


def is_mouse(device: InputDevice) -> bool:
    caps = device.capabilities()
    keys = set(caps.get(ecodes.EV_KEY, []))
    rels = set(caps.get(ecodes.EV_REL, []))
    return ecodes.REL_X in rels and ecodes.REL_Y in rels and ecodes.BTN_LEFT in keys


def matching_device_paths(kind: str) -> list[str]:
    if not os.path.isdir("/dev/input"):
        return []

    result: list[str] = []
    for name in sorted(fn for fn in os.listdir("/dev/input") if fn.startswith("event")):
        path = f"/dev/input/{name}"
        try:
            device = InputDevice(path)
        except OSError:
            continue
        try:
            if kind == "keyboard" and is_keyboard(device):
                result.append(path)
            elif kind == "mouse" and is_mouse(device):
                result.append(path)
        finally:
            device.close()
    return result


def resolve_key_code(key: Any) -> int:
    if isinstance(key, int):
        return key
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string or integer")

    normalized = key.strip().upper()
    candidates = [normalized]
    if not normalized.startswith("KEY_"):
        candidates.append(f"KEY_{normalized}")

    for candidate in candidates:
        code = ecodes.ecodes.get(candidate)
        if isinstance(code, int):
            return code
    raise ValueError(f"unsupported key: {key}")


def resolve_button_code(button: Any) -> int:
    if isinstance(button, int):
        return button
    if not isinstance(button, str) or not button.strip():
        raise ValueError("button must be a non-empty string or integer")

    normalized = button.strip().lower()
    code = BUTTON_NAME_MAP.get(normalized)
    if code is not None:
        return code

    upper_name = button.strip().upper()
    candidates = [upper_name]
    if not upper_name.startswith("BTN_"):
        candidates.append(f"BTN_{upper_name}")

    for candidate in candidates:
        code = ecodes.ecodes.get(candidate)
        if isinstance(code, int):
            return code
    raise ValueError(f"unsupported mouse button: {button}")


def normalize_key_name(key_code: int) -> str:
    name = ecodes.KEY.get(key_code)
    if isinstance(name, list):
        return str(name[0])
    if isinstance(name, str):
        return name
    return f"KEY_{key_code}"


def normalize_button_name(button_code: int) -> str:
    if button_code == ecodes.BTN_LEFT:
        return "BTN_LEFT"
    if button_code == ecodes.BTN_RIGHT:
        return "BTN_RIGHT"
    if button_code == ecodes.BTN_MIDDLE:
        return "BTN_MIDDLE"
    name = ecodes.BTN.get(button_code)
    if isinstance(name, list):
        return str(name[0])
    if isinstance(name, str):
        return name
    return f"BTN_{button_code}"


@dataclass
class KeyboardState:
    modifiers: int = 0
    pressed_keys: list[int] = field(default_factory=list)

    def update(self, key_code: int, is_pressed: bool) -> bool:
        if key_code in MODIFIER_MAP:
            mask = MODIFIER_MAP[key_code]
            if is_pressed:
                self.modifiers |= mask
            else:
                self.modifiers &= ~mask
            return True

        hid_code = KEY_MAP.get(key_code)
        if hid_code is None:
            return False

        if is_pressed:
            if hid_code not in self.pressed_keys:
                self.pressed_keys.append(hid_code)
        else:
            self.pressed_keys = [code for code in self.pressed_keys if code != hid_code]
        return True

    def to_report(self) -> bytes:
        if len(self.pressed_keys) > 6:
            keys = [0x01, 0x00, 0x00, 0x00, 0x00, 0x00]
        else:
            keys = self.pressed_keys[:6] + [0x00] * (6 - len(self.pressed_keys))
        return bytes([self.modifiers, 0x00, *keys])


@dataclass
class MouseState:
    buttons: int = 0
    rel_x: int = 0
    rel_y: int = 0
    wheel: int = 0

    def update_button(self, key_code: int, is_pressed: bool) -> bool:
        mask = MOUSE_BUTTON_MAP.get(key_code)
        if mask is None:
            return False
        if is_pressed:
            self.buttons |= mask
        else:
            self.buttons &= ~mask
        return True

    def update_rel(self, code: int, value: int) -> bool:
        if code == ecodes.REL_X:
            self.rel_x += int(value)
            return True
        if code == ecodes.REL_Y:
            self.rel_y += int(value)
            return True
        if code == ecodes.REL_WHEEL:
            self.wheel += int(value)
            return True
        return False

    def clear_motion(self) -> None:
        self.rel_x = 0
        self.rel_y = 0
        self.wheel = 0

    def flush_reports(self) -> list[bytes]:
        pending_x = int(self.rel_x)
        pending_y = int(self.rel_y)
        pending_wheel = int(self.wheel)
        reports: list[bytes] = []
        if pending_x == 0 and pending_y == 0 and pending_wheel == 0:
            reports.append(bytes([self.buttons, 0x00, 0x00, 0x00]))
        else:
            while pending_x != 0 or pending_y != 0 or pending_wheel != 0:
                step_x = clamp_rel(pending_x)
                step_y = clamp_rel(pending_y)
                step_wheel = clamp_rel(pending_wheel)
                reports.append(
                    bytes([
                        self.buttons,
                        signed_byte(step_x),
                        signed_byte(step_y),
                        signed_byte(step_wheel),
                    ])
                )
                pending_x -= step_x
                pending_y -= step_y
                pending_wheel -= step_wheel
        self.clear_motion()
        return reports


class Forwarder:
    def __init__(
        self,
        keyboard_paths: list[str],
        mouse_paths: list[str],
        grab: bool,
        scan_interval: float,
        keyboard_out_path: str,
        mouse_out_path: str,
        control_socket_path: str | None,
        verbose: bool = False,
    ) -> None:
        self.selector = selectors.DefaultSelector()
        self.keyboard_paths = keyboard_paths
        self.mouse_paths = mouse_paths
        self.grab = grab
        self.scan_interval = scan_interval
        self.keyboard_out_path = keyboard_out_path
        self.mouse_out_path = mouse_out_path
        self.control_socket_path = control_socket_path
        self.verbose = verbose
        self.keyboard_state = KeyboardState()
        self.mouse_state = MouseState()
        self.keyboard_devices: dict[str, InputDevice] = {}
        self.mouse_devices: dict[str, InputDevice] = {}
        self.keyboard_out = None
        self.mouse_out = None
        self.control_socket: socket.socket | None = None
        self.outputs_ready = False
        self.running = True
        self.next_scan_at = 0.0
        self.next_output_retry_at = 0.0
        self.pressed_key_codes: set[int] = set()
        self.pressed_button_codes: set[int] = set()
        self.open_outputs()

    def log_verbose(self, message: str) -> None:
        if self.verbose:
            print(message)

    def log_error(self, message: str) -> None:
        print(message)

    def target_paths(self, kind: str) -> set[str]:
        configured = self.keyboard_paths if kind == "keyboard" else self.mouse_paths
        if configured:
            return {path for path in configured if os.path.exists(path)}
        return set(matching_device_paths(kind))

    def device_map(self, kind: str) -> dict[str, InputDevice]:
        return self.keyboard_devices if kind == "keyboard" else self.mouse_devices

    def reset_kind_state(self, kind: str) -> None:
        if kind == "keyboard":
            self.keyboard_state = KeyboardState()
            self.pressed_key_codes.clear()
            self.send_keyboard_report()
        else:
            self.mouse_state = MouseState()
            self.pressed_button_codes.clear()
            self.send_mouse_report()

    def set_key_pressed(self, key_code: int, is_pressed: bool) -> None:
        if is_pressed:
            self.pressed_key_codes.add(key_code)
        else:
            self.pressed_key_codes.discard(key_code)

    def set_button_pressed(self, button_code: int, is_pressed: bool) -> None:
        if is_pressed:
            self.pressed_button_codes.add(button_code)
        else:
            self.pressed_button_codes.discard(button_code)

    def get_state_snapshot(self) -> dict[str, Any]:
        return {
            "type": "state",
            "keys": [normalize_key_name(code) for code in sorted(self.pressed_key_codes)],
            "mouse_buttons": [normalize_button_name(code) for code in sorted(self.pressed_button_codes)],
        }

    def send_control_response(self, reply_target: str, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        if not reply_target:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(json.dumps(payload, ensure_ascii=True).encode("utf-8"), reply_target)
        finally:
            sock.close()

    def register_device(self, kind: str, path: str) -> None:
        try:
            device = InputDevice(path)
        except OSError:
            return

        matcher = is_keyboard if kind == "keyboard" else is_mouse
        try:
            if not matcher(device):
                device.close()
                return
            if self.grab:
                device.grab()
            self.selector.register(device.fd, selectors.EVENT_READ, (kind, device))
        except OSError:
            try:
                device.close()
            except OSError:
                pass
            return

        self.device_map(kind)[path] = device
        self.log_verbose(f"[{kind}] attached {describe_capabilities(device)}")

    def unregister_device(self, kind: str, path: str, reset_state: bool) -> None:
        device = self.device_map(kind).pop(path, None)
        if device is None:
            return

        try:
            self.selector.unregister(device.fd)
        except Exception:
            pass
        try:
            if self.grab:
                device.ungrab()
        except OSError:
            pass
        try:
            device.close()
        except OSError:
            pass

        if reset_state:
            self.reset_kind_state(kind)
        self.next_scan_at = 0.0
        self.log_verbose(f"[{kind}] detached path={path}")

    def refresh_devices(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self.keyboard_devices and self.mouse_devices:
            return
        if not force and now < self.next_scan_at:
            return
        self.next_scan_at = now + self.scan_interval

        for kind in ("keyboard", "mouse"):
            devices = self.device_map(kind)
            wanted = self.target_paths(kind)

            for path in sorted(set(devices) - wanted):
                self.unregister_device(kind, path, reset_state=True)
            for path in sorted(wanted - set(devices)):
                self.register_device(kind, path)

    def setup_control_socket(self) -> None:
        if not self.control_socket_path:
            return

        parent = os.path.dirname(self.control_socket_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.control_socket_path):
            os.unlink(self.control_socket_path)

        control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        control_socket.bind(self.control_socket_path)
        os.chmod(self.control_socket_path, 0o666)
        self.selector.register(control_socket.fileno(), selectors.EVENT_READ, ("control", control_socket))
        self.control_socket = control_socket
        self.log_verbose(f"Control socket: {self.control_socket_path}")
        if self.verbose:
            print("Control examples:")
            for example in COMMAND_HELP.values():
                print(f"  {json.dumps(example, ensure_ascii=True)}")

    def setup(self) -> None:
        self.refresh_devices(force=True)
        self.setup_control_socket()

    def close_outputs(self) -> None:
        for stream_name in ("keyboard_out", "mouse_out"):
            stream = getattr(self, stream_name)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
                setattr(self, stream_name, None)
        self.outputs_ready = False

    def open_outputs(self) -> bool:
        try:
            self.keyboard_out = os.fdopen(os.open(self.keyboard_out_path, os.O_WRONLY), "wb", buffering=0)
            self.mouse_out = os.fdopen(os.open(self.mouse_out_path, os.O_WRONLY), "wb", buffering=0)
        except OSError as exc:
            self.close_outputs()
            now = time.monotonic()
            if now >= self.next_output_retry_at:
                self.log_error(f"[gadget] waiting for hidg endpoints: {exc}")
                self.next_output_retry_at = now + self.scan_interval
            return False

        self.outputs_ready = True
        self.next_output_retry_at = 0.0
        return True

    def ensure_outputs(self) -> bool:
        if self.outputs_ready and self.keyboard_out is not None and self.mouse_out is not None:
            return True
        return self.open_outputs()

    def handle_output_error(self, channel: str, exc: OSError) -> None:
        self.log_error(f"[gadget] {channel} output unavailable: {exc}")
        self.close_outputs()
        self.next_output_retry_at = time.monotonic() + self.scan_interval

    def teardown(self) -> None:
        for path in list(self.keyboard_devices):
            self.unregister_device("keyboard", path, reset_state=False)
        for path in list(self.mouse_devices):
            self.unregister_device("mouse", path, reset_state=False)
        if self.control_socket is not None:
            try:
                self.selector.unregister(self.control_socket.fileno())
            except Exception:
                pass
            try:
                self.control_socket.close()
            except OSError:
                pass
            self.control_socket = None
        if self.control_socket_path and os.path.exists(self.control_socket_path):
            os.unlink(self.control_socket_path)
        self.close_outputs()

    def stop(self, *_args: object) -> None:
        self.running = False

    def send_keyboard_report(self) -> None:
        if not self.ensure_outputs():
            return
        try:
            self.keyboard_out.write(self.keyboard_state.to_report())
        except OSError as exc:
            self.handle_output_error("keyboard", exc)

    def send_mouse_report(self) -> None:
        if not self.ensure_outputs():
            self.mouse_state.clear_motion()
            return
        try:
            for report in self.mouse_state.flush_reports():
                self.mouse_out.write(report)
        except OSError as exc:
            self.handle_output_error("mouse", exc)

    def apply_key_action(self, key_code: int, action: str) -> None:
        normalized = action.lower()
        if normalized == "tap":
            self.set_key_pressed(key_code, True)
            if self.keyboard_state.update(key_code, True):
                self.send_keyboard_report()
            self.set_key_pressed(key_code, False)
            if self.keyboard_state.update(key_code, False):
                self.send_keyboard_report()
            return

        if normalized not in {"down", "up"}:
            raise ValueError("key action must be down, up, or tap")
        self.set_key_pressed(key_code, normalized == "down")
        if self.keyboard_state.update(key_code, normalized == "down"):
            self.send_keyboard_report()

    def apply_mouse_move(self, dx: int = 0, dy: int = 0, wheel: int = 0) -> None:
        if dx:
            self.mouse_state.update_rel(ecodes.REL_X, int(dx))
        if dy:
            self.mouse_state.update_rel(ecodes.REL_Y, int(dy))
        if wheel:
            self.mouse_state.update_rel(ecodes.REL_WHEEL, int(wheel))
        self.send_mouse_report()

    def apply_mouse_button(self, button_code: int, action: str) -> None:
        normalized = action.lower()
        if normalized == "click":
            self.set_button_pressed(button_code, True)
            if self.mouse_state.update_button(button_code, True):
                self.send_mouse_report()
            self.set_button_pressed(button_code, False)
            if self.mouse_state.update_button(button_code, False):
                self.send_mouse_report()
            return

        if normalized not in {"down", "up"}:
            raise ValueError("mouse button action must be down, up, or click")
        self.set_button_pressed(button_code, normalized == "down")
        if self.mouse_state.update_button(button_code, normalized == "down"):
            self.send_mouse_report()

    def release_all(self) -> None:
        self.keyboard_state = KeyboardState()
        self.mouse_state = MouseState()
        self.pressed_key_codes.clear()
        self.pressed_button_codes.clear()
        self.send_keyboard_report()
        self.send_mouse_report()

    def process_control_command(self, command: dict[str, Any]) -> dict[str, Any] | None:
        command_type = str(command.get("type", "")).strip().lower()
        if not command_type:
            raise ValueError("command missing type")

        if command_type == "key":
            key_code = resolve_key_code(command.get("key"))
            action = str(command.get("action", "tap"))
            self.apply_key_action(key_code, action)
            return {"ok": True, "type": "key", "key": normalize_key_name(key_code), "action": action.lower()}

        if command_type == "mouse_move":
            dx = int(command.get("dx", 0))
            dy = int(command.get("dy", 0))
            wheel = int(command.get("wheel", 0))
            self.apply_mouse_move(dx=dx, dy=dy, wheel=wheel)
            return {"ok": True, "type": "mouse_move", "dx": dx, "dy": dy, "wheel": wheel}

        if command_type == "mouse_button":
            button_code = resolve_button_code(command.get("button"))
            action = str(command.get("action", "click"))
            self.apply_mouse_button(button_code, action)
            return {"ok": True, "type": "mouse_button", "button": normalize_button_name(button_code), "action": action.lower()}

        if command_type == "mouse_scroll":
            wheel = int(command.get("wheel", 0))
            self.apply_mouse_move(wheel=wheel)
            return {"ok": True, "type": "mouse_scroll", "wheel": wheel}

        if command_type == "release_all":
            self.release_all()
            return {"ok": True, "type": "release_all"}

        if command_type == "key_state":
            key_code = resolve_key_code(command.get("key"))
            return {
                "ok": True,
                "type": "key_state",
                "key": normalize_key_name(key_code),
                "pressed": key_code in self.pressed_key_codes,
            }

        if command_type == "mouse_button_state":
            button_code = resolve_button_code(command.get("button"))
            return {
                "ok": True,
                "type": "mouse_button_state",
                "button": normalize_button_name(button_code),
                "pressed": button_code in self.pressed_button_codes,
            }

        if command_type == "get_state":
            snapshot = self.get_state_snapshot()
            snapshot["ok"] = True
            return snapshot

        raise ValueError(f"unsupported command type: {command_type}")

    def handle_control_socket(self) -> None:
        if self.control_socket is None:
            return
        try:
            payload, sender_addr = self.control_socket.recvfrom(65535)
        except OSError as exc:
            self.log_error(f"[control] recv error: {exc}")
            return

        try:
            message = json.loads(payload.decode("utf-8"))
            commands = message if isinstance(message, list) else [message]
            responses: list[dict[str, Any]] = []
            reply_target = sender_addr if isinstance(sender_addr, str) else ""
            for command in commands:
                if not isinstance(command, dict):
                    raise ValueError("control command must be a JSON object")
                command_reply_target = str(command.get("reply_socket") or reply_target or "")
                response = self.process_control_command(command)
                if response is not None and command_reply_target:
                    responses.append(response)
                    reply_target = command_reply_target
            if responses and reply_target:
                payload_out = responses[0] if len(responses) == 1 else responses
                self.send_control_response(reply_target, payload_out)
        except Exception as exc:
            self.log_error(f"[control] invalid command: {exc}")

    def handle_keyboard_event(self, event) -> None:
        if event.type != ecodes.EV_KEY or event.value == 2:
            return
        self.set_key_pressed(event.code, event.value == 1)
        changed = self.keyboard_state.update(event.code, event.value == 1)
        if changed:
            self.send_keyboard_report()

    def handle_mouse_event(self, event) -> None:
        if event.type == ecodes.EV_KEY and event.value != 2:
            self.set_button_pressed(event.code, event.value == 1)
            changed = self.mouse_state.update_button(event.code, event.value == 1)
            if changed:
                self.send_mouse_report()
            return
        if event.type == ecodes.EV_REL:
            self.mouse_state.update_rel(event.code, event.value)
            return
        if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
            if any((self.mouse_state.rel_x, self.mouse_state.rel_y, self.mouse_state.wheel)):
                self.send_mouse_report()

    def handle_ready_device(self, kind: str, device: InputDevice) -> None:
        try:
            events = device.read()
            for event in events:
                if kind == "keyboard":
                    self.handle_keyboard_event(event)
                else:
                    self.handle_mouse_event(event)
        except OSError:
            self.unregister_device(kind, device.path, reset_state=True)

    def run(self) -> None:
        self.setup()
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        self.log_verbose("Forwarding started. Press Ctrl+C to stop.")
        if self.keyboard_paths:
            self.log_verbose(f"Keyboard watch list: {self.keyboard_paths}")
        if self.mouse_paths:
            self.log_verbose(f"Mouse watch list: {self.mouse_paths}")
        if not self.keyboard_devices:
            self.log_verbose("No keyboard attached yet. Waiting for hotplug.")
        if not self.mouse_devices:
            self.log_verbose("No mouse attached yet. Waiting for hotplug.")

        try:
            while self.running:
                self.refresh_devices()
                self.ensure_outputs()
                for key, _mask in self.selector.select(timeout=0.2):
                    kind, device = key.data
                    if kind == "control":
                        self.handle_control_socket()
                    else:
                        self.handle_ready_device(kind, device)
        finally:
            try:
                self.keyboard_state = KeyboardState()
                self.mouse_state = MouseState()
                self.send_keyboard_report()
                self.send_mouse_report()
            except OSError:
                pass
            self.teardown()


def list_devices() -> int:
    print("Keyboard candidates:")
    for path in matching_device_paths("keyboard"):
        device = InputDevice(path)
        print(f"  {describe_capabilities(device)}")
        device.close()
    print("Mouse candidates:")
    for path in matching_device_paths("mouse"):
        device = InputDevice(path)
        print(f"  {describe_capabilities(device)}")
        device.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward local keyboard/mouse events to USB HID Gadget.")
    parser.add_argument("--list", action="store_true", help="List detected keyboard and mouse devices.")
    parser.add_argument("--grab", action="store_true", help="Grab input devices exclusively.")
    parser.add_argument("--keyboard-path", action="append", default=[], help="Explicit keyboard event path.")
    parser.add_argument("--mouse-path", action="append", default=[], help="Explicit mouse event path.")
    parser.add_argument("--scan-interval", type=float, default=1.0, help="How often to rescan /dev/input for hotplug devices.")
    parser.add_argument("--keyboard-out", default="/dev/hidg0", help="Keyboard HID gadget character device.")
    parser.add_argument("--mouse-out", default="/dev/hidg1", help="Mouse HID gadget character device.")
    parser.add_argument("--control-socket", default="/tmp/km_passthrough.sock", help="Unix datagram socket for external control.")
    parser.add_argument("--disable-control-socket", action="store_true", help="Disable the external control socket.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose passthrough logs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list:
        return list_devices()

    if os.geteuid() != 0:
        print("Run as root so the program can access /dev/input/event* and /dev/hidg*.", file=sys.stderr)
        return 1

    if not os.path.exists(args.keyboard_out) or not os.path.exists(args.mouse_out):
        print(f"Missing {args.keyboard_out} or {args.mouse_out}. Run the gadget setup script first.", file=sys.stderr)
        return 1

    if args.scan_interval <= 0:
        print("--scan-interval must be greater than 0.", file=sys.stderr)
        return 1

    control_socket_path = None if args.disable_control_socket else args.control_socket
    forwarder = Forwarder(
        args.keyboard_path,
        args.mouse_path,
        args.grab,
        args.scan_interval,
        args.keyboard_out,
        args.mouse_out,
        control_socket_path,
        args.verbose,
    )
    forwarder.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
