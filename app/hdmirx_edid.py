#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from generate_hdmirx_edid import build_edid, make_random_name, make_random_vendor, parse_mode
from inspect_hdmirx_edid import inspect_edid


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EDID_FILE = ROOT / "generated" / "hdmirx_custom_edid.bin"
DEFAULT_IDENTITY_FILE = ROOT / "config" / "hdmirx_edid_identity.json"
DEFAULT_STATE_FILE = ROOT / "config" / "hdmirx_edid_state.json"
DEFAULT_DEVICE = os.environ.get("HDMIRX_DEVICE", "").strip()
DEFAULT_IDENTITY = {
    "brand": "OrangePi",
    "name": "OPI-COMPAT",
    "vendor": "OPI",
    "product_id": "0x3588",
    "serial": "0x20260414",
}


@dataclass(frozen=True)
class EdidProfile:
    name: str
    label: str
    mode: str
    description: str
    default: bool = False
    visible: bool = True
    extra_modes: tuple[str, ...] = ()

    def all_modes(self) -> tuple[str, ...]:
        return (self.mode, *self.extra_modes)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "mode": self.mode,
            "modes": list(self.all_modes()),
            "description": self.description,
            "default": self.default,
            "visible": self.visible,
        }


EDID_PROFILES: tuple[EdidProfile, ...] = (
    EdidProfile(
        "standard-dual",
        "标准显示器",
        "1080p120",
        "优先向主机声明标准 1920x1080@120，并同时提供 2560x1440@60 作为第二选项。CTA 中保留标准 1080p120 SVD/VIC 声明。",
        True,
        True,
        ("1440p60",),
    ),
    EdidProfile(
        "single-1440p60",
        "2k60 兼容模式",
        "1440p60",
        "单模式 EDID，只向主机声明 2560x1440@60。作为第二优先的稳定模式保留在前端。",
    ),
    EdidProfile(
        "single-1080p120-compat",
        "1080p120 兼容模式",
        "1080p120compat",
        "仿照当前可工作的 1440p60 EDID 结构生成：只声明 DTD，不声明 CTA SVD，porch/sync 与 1440p60 保持同一风格。",
        visible=False,
    ),
    EdidProfile(
        "single-1080p60",
        "1080p60",
        "1080p60",
        "单模式 EDID，只向主机声明 1920x1080@60。当前在 OrangePi HDMI RX 链路上保留为后台调试预设。",
        visible=False,
    ),
    EdidProfile(
        "single-1080p60-compat",
        "1080p60 兼容实验",
        "1080p60compat",
        "仿照当前可工作的 1440p60 EDID 结构生成：只声明 DTD，不声明 CTA SVD，porch/sync 与 1440p60 保持同一风格。",
        visible=False,
    ),
    EdidProfile(
        "single-1080p90",
        "1080p90",
        "1080p90",
        "单模式 EDID，只向主机声明 1920x1080@90。当前在 OrangePi HDMI RX 链路上保留为后台调试预设。",
        visible=False,
    ),
    EdidProfile(
        "single-1080p120",
        "1080p120",
        "1080p120",
        "单模式 EDID，只向主机声明 1920x1080@120。当前在 OrangePi HDMI RX 链路上保留为后台调试预设。",
        visible=False,
    ),
)
EDID_PROFILE_MAP = {profile.name: profile for profile in EDID_PROFILES}
DEFAULT_PROFILE_NAME = next(profile.name for profile in EDID_PROFILES if profile.default)


def profile_payload(visible_only: bool = False) -> list[dict[str, object]]:
    profiles = EDID_PROFILES
    if visible_only:
        profiles = tuple(profile for profile in profiles if profile.visible)
    return [profile.to_dict() for profile in profiles]


def format_hex(value: int, width: int) -> str:
    return f"0x{int(value) & ((1 << (width * 4)) - 1):0{width}X}"


def parse_int(value: object, fallback: int) -> int:
    try:
        return int(str(value), 0)
    except Exception:
        return fallback


def sanitize_edid_identity(payload: dict[str, object] | None) -> dict[str, str]:
    data = dict(DEFAULT_IDENTITY)
    if isinstance(payload, dict):
        data.update({key: value for key, value in payload.items() if value is not None})

    brand = "".join(ch for ch in str(data.get("brand", "")).strip() if 32 <= ord(ch) <= 126)[:24] or DEFAULT_IDENTITY["brand"]
    name = "".join(ch for ch in str(data.get("name", "")).strip() if 32 <= ord(ch) <= 126)[:13] or DEFAULT_IDENTITY["name"]
    vendor = "".join(ch for ch in str(data.get("vendor", "")).strip().upper() if "A" <= ch <= "Z")[:3]
    if len(vendor) != 3:
        vendor = DEFAULT_IDENTITY["vendor"]

    product_id = parse_int(data.get("product_id"), int(DEFAULT_IDENTITY["product_id"], 0)) & 0xFFFF
    serial = parse_int(data.get("serial"), int(DEFAULT_IDENTITY["serial"], 0)) & 0xFFFFFFFF
    return {
        "brand": brand,
        "name": name,
        "vendor": vendor,
        "product_id": format_hex(product_id, 4),
        "serial": format_hex(serial, 8),
    }


def load_identity(path: Path = DEFAULT_IDENTITY_FILE) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return sanitize_edid_identity(payload if isinstance(payload, dict) else {})


def save_identity(identity: dict[str, object], path: Path = DEFAULT_IDENTITY_FILE) -> dict[str, str]:
    payload = sanitize_edid_identity(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return payload


def random_identity(path: Path = DEFAULT_IDENTITY_FILE) -> dict[str, str]:
    import random

    vendor = make_random_vendor()
    brand_suffix = "".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(4))
    return save_identity(
        {
            "brand": f"Panel-{brand_suffix}",
            "name": make_random_name(vendor),
            "vendor": vendor,
            "product_id": random.randint(0x0001, 0xFFFF),
            "serial": random.randint(0x00000001, 0xFFFFFFFF),
        },
        path,
    )


def load_edid_state(path: Path = DEFAULT_STATE_FILE) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def save_edid_state(payload: dict[str, object], path: Path = DEFAULT_STATE_FILE) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return payload


def remember_active_profile(profile_name: str, path: Path = DEFAULT_STATE_FILE) -> dict[str, object]:
    state = load_edid_state(path)
    state["active_profile"] = str(profile_name).strip() or DEFAULT_PROFILE_NAME
    return save_edid_state(state, path)


def current_active_profile(path: Path = DEFAULT_STATE_FILE) -> str:
    state = load_edid_state(path)
    value = str(state.get("active_profile", "")).strip()
    return value or DEFAULT_PROFILE_NAME


def get_profile(name: str) -> EdidProfile:
    normalized = (name or DEFAULT_PROFILE_NAME).strip()
    try:
        return EDID_PROFILE_MAP[normalized]
    except KeyError as exc:
        supported = ", ".join(profile.name for profile in EDID_PROFILES)
        raise SystemExit(f"Unsupported EDID profile: {normalized}. Supported: {supported}") from exc


def need_cmd(command: str) -> None:
    if shutil.which(command) is None:
        raise SystemExit(f"Missing command: {command}")


def list_video_devices() -> list[str]:
    completed = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True)
    if completed.returncode != 0:
        return []
    devices: list[str] = []
    for line in completed.stdout.splitlines():
        value = line.strip()
        if value.startswith("/dev/video"):
            devices.append(value)
    return devices


def score_device(device: str) -> int:
    completed = subprocess.run(["v4l2-ctl", "-d", device, "--all"], capture_output=True, text=True)
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    score = 0
    if "hdmi" in output:
        score += 5
    if "rx" in output or "hdmirx" in output:
        score += 5
    if "dv timings" in output:
        score += 3
    formats = subprocess.run(["v4l2-ctl", "-d", device, "--list-formats-ext"], capture_output=True, text=True)
    if formats.returncode == 0:
        score += 1
    return score


def resolve_device() -> str:
    if DEFAULT_DEVICE:
        return DEFAULT_DEVICE
    best_device = ""
    best_score = -1
    for device in list_video_devices():
        score = score_device(device)
        if score > best_score:
            best_device = device
            best_score = score
    return best_device


def write_profile_edid(profile: EdidProfile, output: Path) -> bytes:
    modes = []
    seen: set[str] = set()
    for token in profile.all_modes():
        mode = parse_mode(token)
        if mode.token in seen:
            continue
        seen.add(mode.token)
        modes.append(mode)
    identity = load_identity()
    name = os.environ.get("HDMIRX_COMPAT_EDID_NAME", identity["name"]).strip() or identity["name"]
    vendor = os.environ.get("HDMIRX_COMPAT_EDID_VENDOR", identity["vendor"]).strip().upper() or identity["vendor"]
    product_id = int(os.environ.get("HDMIRX_COMPAT_EDID_PRODUCT_ID", identity["product_id"]), 0)
    serial = int(os.environ.get("HDMIRX_COMPAT_EDID_SERIAL", identity["serial"]), 0)
    edid = build_edid(name=name, vendor=vendor, product_id=product_id, serial=serial, modes=modes)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(edid)
    return edid


def print_inspection(edid: bytes, output: Path) -> None:
    errors, warnings, summary = inspect_edid(edid)
    print(f"EDID: {output}")
    print(f"Bytes: {len(edid)}")
    print(f"Blocks: {len(edid) // 128 if len(edid) % 128 == 0 else 'invalid'}")
    if summary:
        print("摘要:")
        for item in summary:
            print(f"  {item}")
    if errors:
        print("错误:")
        for item in errors:
            print(f"  - {item}")
    if warnings:
        print("警告:")
        for item in warnings:
            print(f"  - {item}")
    if errors:
        raise SystemExit("EDID inspection failed")
    if not warnings:
        print("检查结果: 通过")


def apply_profile(profile_name: str, output: Path, device: str | None) -> None:
    need_cmd("v4l2-ctl")
    profile = get_profile(profile_name)
    resolved_device = (device or resolve_device()).strip()
    if not resolved_device:
        raise SystemExit("No HDMI RX V4L2 device detected. Set HDMIRX_DEVICE=/dev/videoX if auto-detect is wrong.")

    print(f"Applying HDMI RX EDID profile: {profile.name} ({profile.label})")
    edid = write_profile_edid(profile, output)
    identity = load_identity()
    print(f"Wrote EDID: {output}")
    print(f"Brand: {identity['brand']}")
    print(f"Display name: {os.environ.get('HDMIRX_COMPAT_EDID_NAME', identity['name'])}")
    print(f"Vendor: {os.environ.get('HDMIRX_COMPAT_EDID_VENDOR', identity['vendor'])}")
    print(f"Product ID: {os.environ.get('HDMIRX_COMPAT_EDID_PRODUCT_ID', identity['product_id'])}")
    print(f"Serial: {os.environ.get('HDMIRX_COMPAT_EDID_SERIAL', identity['serial'])}")
    print(f"Modes: {', '.join(profile.all_modes())}")
    print_inspection(edid, output)

    command = [
        "v4l2-ctl",
        "-d",
        resolved_device,
        f"--set-edid=pad=0,file={output},format=raw",
        "--fix-edid-checksums",
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip())
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    remember_active_profile(profile.name)
    print(f"Custom EDID pushed to {resolved_device}. Replug HDMI or force the host to redetect this display.")


def print_status(device: str | None, output: Path) -> None:
    need_cmd("v4l2-ctl")
    resolved_device = (device or resolve_device()).strip()
    print(f"Device: {resolved_device or 'not found'}")
    print(f"EDID file: {output}")
    print(f"Active profile: {current_active_profile()}")
    print(f"Identity: {json.dumps(load_identity(), ensure_ascii=False)}")
    print("Profiles:")
    for profile in EDID_PROFILES:
        mark = "*" if profile.default else "-"
        print(f"  {mark} {profile.name}: {profile.label} ({profile.mode})")
    if output.exists():
        print()
        print_inspection(output.read_bytes(), output)
    if resolved_device:
        print()
        subprocess.run(["v4l2-ctl", "-d", resolved_device, "--get-dv-timings"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OrangePi HDMI RX EDID profile controller")
    parser.add_argument("--device", default="", help="HDMI RX V4L2 device, e.g. /dev/video0")
    parser.add_argument("--output", default=str(DEFAULT_EDID_FILE), help="Raw EDID output file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List EDID profiles as JSON")
    sub.add_parser("identity", help="Show current persistent EDID identity as JSON")
    sub.add_parser("random-identity", help="Persist a new random EDID identity")
    sub.add_parser("current-profile", help="Print the last successfully applied EDID profile")
    apply_parser = sub.add_parser("apply", help="Generate and apply one EDID profile")
    apply_parser.add_argument("profile", nargs="?", default=DEFAULT_PROFILE_NAME)
    sub.add_parser("status", help="Show current EDID status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output).expanduser().resolve()
    if args.command == "list":
        print(json.dumps({"profiles": profile_payload(), "default": DEFAULT_PROFILE_NAME}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "identity":
        print(json.dumps(load_identity(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "random-identity":
        print(json.dumps(random_identity(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "current-profile":
        print(current_active_profile())
        return 0
    if args.command == "apply":
        apply_profile(args.profile, output, args.device)
        return 0
    if args.command == "status":
        print_status(args.device, output)
        return 0
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
