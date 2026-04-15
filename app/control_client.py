#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Any


class PassthroughController:
    def __init__(self, socket_path: str = "/tmp/km_passthrough.sock") -> None:
        self.socket_path = socket_path

    def send(self, command: dict[str, Any] | list[dict[str, Any]]) -> None:
        payload = json.dumps(command, ensure_ascii=True).encode("utf-8")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(payload, self.socket_path)
        finally:
            sock.close()

    def request(self, command: dict[str, Any], timeout: float = 1.0) -> dict[str, Any] | list[dict[str, Any]]:
        reply_dir = tempfile.gettempdir()
        reply_path = os.path.join(reply_dir, f"km_passthrough_client_{os.getpid()}_{time.time_ns()}.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.bind(reply_path)
            sock.settimeout(timeout)
            payload = dict(command)
            payload["reply_socket"] = reply_path
            sock.sendto(json.dumps(payload, ensure_ascii=True).encode("utf-8"), self.socket_path)
            data, _addr = sock.recvfrom(65535)
            return json.loads(data.decode("utf-8"))
        finally:
            sock.close()
            if os.path.exists(reply_path):
                os.unlink(reply_path)

    def key_down(self, key: str) -> None:
        self.send({"type": "key", "key": key, "action": "down"})

    def key_up(self, key: str) -> None:
        self.send({"type": "key", "key": key, "action": "up"})

    def key_tap(self, key: str) -> None:
        self.send({"type": "key", "key": key, "action": "tap"})

    def is_key_down(self, key: str, timeout: float = 1.0) -> bool:
        response = self.request({"type": "key_state", "key": key}, timeout=timeout)
        if not isinstance(response, dict):
            raise RuntimeError(f"unexpected response: {response!r}")
        return bool(response.get("pressed", False))

    def mouse_move(self, dx: int = 0, dy: int = 0) -> None:
        self.send({"type": "mouse_move", "dx": dx, "dy": dy})

    def mouse_scroll(self, wheel: int) -> None:
        self.send({"type": "mouse_scroll", "wheel": wheel})

    def mouse_down(self, button: str = "left") -> None:
        self.send({"type": "mouse_button", "button": button, "action": "down"})

    def mouse_up(self, button: str = "left") -> None:
        self.send({"type": "mouse_button", "button": button, "action": "up"})

    def mouse_click(self, button: str = "left") -> None:
        self.send({"type": "mouse_button", "button": button, "action": "click"})

    def is_mouse_down(self, button: str = "left", timeout: float = 1.0) -> bool:
        response = self.request({"type": "mouse_button_state", "button": button}, timeout=timeout)
        if not isinstance(response, dict):
            raise RuntimeError(f"unexpected response: {response!r}")
        return bool(response.get("pressed", False))

    def get_state(self, timeout: float = 1.0) -> dict[str, Any]:
        response = self.request({"type": "get_state"}, timeout=timeout)
        if not isinstance(response, dict):
            raise RuntimeError(f"unexpected response: {response!r}")
        return response

    def release_all(self) -> None:
        self.send({"type": "release_all"})


if __name__ == "__main__":
    controller = PassthroughController()
    if not Path(controller.socket_path).exists():
        raise SystemExit(f"Control socket not found: {controller.socket_path}")
    print(controller.get_state())
