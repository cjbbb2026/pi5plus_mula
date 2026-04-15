#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import random
import string
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoMode:
    token: str
    width: int
    height: int
    refresh: int
    pixel_clock_khz: int
    h_front: int
    h_sync: int
    h_back: int
    v_front: int
    v_sync: int
    v_back: int
    vic: int | None
    hsync_positive: bool = True
    vsync_positive: bool = True

    @property
    def h_blank(self) -> int:
        return self.h_front + self.h_sync + self.h_back

    @property
    def v_blank(self) -> int:
        return self.v_front + self.v_sync + self.v_back

    @property
    def h_total(self) -> int:
        return self.width + self.h_blank

    @property
    def v_total(self) -> int:
        return self.height + self.v_blank

    @property
    def horizontal_khz(self) -> float:
        return float(self.pixel_clock_khz) / max(1.0, float(self.h_total))

    @property
    def vertical_hz(self) -> float:
        return (float(self.pixel_clock_khz) * 1000.0) / max(1.0, float(self.h_total * self.v_total))

    @property
    def pixel_clock_mhz(self) -> float:
        return float(self.pixel_clock_khz) / 1000.0


MODES: dict[str, VideoMode] = {
    '1080p60': VideoMode('1080p60', 1920, 1080, 60, 148500, 88, 44, 148, 4, 5, 36, 16),
    '1080p60compat': VideoMode('1080p60compat', 1920, 1080, 60, 145392, 48, 32, 80, 3, 5, 77, None, True, False),
    '1080p120compat': VideoMode('1080p120compat', 1920, 1080, 120, 290784, 48, 32, 80, 3, 5, 77, None, True, False),
    # Keep high-refresh 1080p on the same conservative porch/sync totals as 1080p60.
    # This is less aggressive than the prior reduced-blanking-like timing and is accepted
    # by a wider range of HDMI sources.
    '1080p90': VideoMode('1080p90', 1920, 1080, 90, 222750, 88, 44, 148, 4, 5, 36, None),
    '1080p120': VideoMode('1080p120', 1920, 1080, 120, 297000, 88, 44, 148, 4, 5, 36, 63),
    '1440p60': VideoMode('1440p60', 2560, 1440, 60, 248875, 48, 32, 80, 3, 5, 77, None, True, False),
}
ALIASES = {
    'fhd60': '1080p60',
    'fhd60compat': '1080p60compat',
    '1080p-compat': '1080p60compat',
    'fhd120compat': '1080p120compat',
    '1080p120-compat': '1080p120compat',
    'fhd90': '1080p90',
    'fhd120': '1080p120',
    '2k60': '1440p60',
    'qhd60': '1440p60',
    '1440p60': '1440p60',
    '1k60': '1080p60',
    '1k60compat': '1080p60compat',
    '1k120compat': '1080p120compat',
    '1k90': '1080p90',
    '1k120': '1080p120',
}


def parse_mode(token: str) -> VideoMode:
    key = token.strip().lower()
    key = ALIASES.get(key, key)
    if key not in MODES:
        raise SystemExit(f'Unsupported mode: {token}. Supported: {", ".join(MODES)}')
    return MODES[key]


def encode_vendor_id(vendor: str) -> bytes:
    vendor = vendor.strip().upper()
    if len(vendor) != 3 or any(not ('A' <= c <= 'Z') for c in vendor):
        raise SystemExit('Vendor must be exactly 3 uppercase letters, e.g. OPI')
    value = ((ord(vendor[0]) - 64) << 10) | ((ord(vendor[1]) - 64) << 5) | (ord(vendor[2]) - 64)
    return bytes([(value >> 8) & 0xFF, value & 0xFF])


def set_checksum(block: bytearray) -> None:
    block[127] = (-sum(block[:127])) & 0xFF


def pack_descriptor(payload: bytes, tag: int) -> bytes:
    data = bytearray(18)
    data[0:2] = b'\x00\x00'
    data[2] = 0x00
    data[3] = tag
    data[4] = 0x00
    body = payload[:13]
    data[5:5 + len(body)] = body
    if len(body) < 13:
        data[5 + len(body)] = 0x0A
        for i in range(6 + len(body), 18):
            data[i] = 0x20
    return bytes(data)


def pack_monitor_name(name: str) -> bytes:
    ascii_name = ''.join(ch for ch in name if 32 <= ord(ch) <= 126).strip() or 'OrangePi HDMI'
    return pack_descriptor(ascii_name.encode('ascii')[:13], 0xFC)


def pack_serial_text(text: str) -> bytes:
    ascii_text = ''.join(ch for ch in text if 32 <= ord(ch) <= 126).strip() or 'OPI00000001'
    return pack_descriptor(ascii_text.encode('ascii')[:13], 0xFF)


def pack_range_limits(modes: list[VideoMode]) -> bytes:
    min_v_hz = max(1, math.floor(min(mode.vertical_hz for mode in modes)) - 1)
    max_v_hz = min(255, math.ceil(max(mode.vertical_hz for mode in modes)) + 1)
    min_h_khz = max(1, math.floor(min(mode.horizontal_khz for mode in modes)) - 1)
    max_h_khz = min(255, math.ceil(max(mode.horizontal_khz for mode in modes)) + 1)
    max_pixel_clock_mhz = max(mode.pixel_clock_mhz for mode in modes)
    max_pixel_clock_10mhz = min(255, max(1, math.ceil(max_pixel_clock_mhz / 10.0)))

    payload = bytearray(13)
    payload[0] = min_v_hz
    payload[1] = max_v_hz
    payload[2] = min_h_khz
    payload[3] = max_h_khz
    payload[4] = max_pixel_clock_10mhz
    payload[5] = 0x0A
    return pack_descriptor(bytes(payload), 0xFD)


def pack_dtd(mode: VideoMode, width_mm: int, height_mm: int) -> bytes:
    h_active = mode.width
    h_blank = mode.h_blank
    v_active = mode.height
    v_blank = mode.v_blank
    h_sync_offset = mode.h_front
    h_sync_width = mode.h_sync
    v_sync_offset = mode.v_front
    v_sync_width = mode.v_sync
    flags = 0x18
    if mode.vsync_positive:
        flags |= 0x04
    if mode.hsync_positive:
        flags |= 0x02

    d = bytearray(18)
    pixel_clock = mode.pixel_clock_khz // 10
    d[0] = pixel_clock & 0xFF
    d[1] = (pixel_clock >> 8) & 0xFF
    d[2] = h_active & 0xFF
    d[3] = h_blank & 0xFF
    d[4] = ((h_active >> 8) & 0xF) << 4 | ((h_blank >> 8) & 0xF)
    d[5] = v_active & 0xFF
    d[6] = v_blank & 0xFF
    d[7] = ((v_active >> 8) & 0xF) << 4 | ((v_blank >> 8) & 0xF)
    d[8] = h_sync_offset & 0xFF
    d[9] = h_sync_width & 0xFF
    d[10] = ((v_sync_offset & 0xF) << 4) | (v_sync_width & 0xF)
    d[11] = ((h_sync_offset >> 8) & 0x3) << 6 | ((h_sync_width >> 8) & 0x3) << 4 | ((v_sync_offset >> 4) & 0x3) << 2 | ((v_sync_width >> 4) & 0x3)
    d[12] = width_mm & 0xFF
    d[13] = height_mm & 0xFF
    d[14] = ((width_mm >> 8) & 0xF) << 4 | ((height_mm >> 8) & 0xF)
    d[15] = 0
    d[16] = 0
    d[17] = flags
    return bytes(d)


def pack_data_block(tag: int, payload: bytes) -> bytes:
    if len(payload) > 31:
        raise SystemExit(f'CTA data block too large: tag={tag} bytes={len(payload)}')
    return bytes([(int(tag) << 5) | len(payload)]) + bytes(payload)


def pack_hdmi_vsdb(max_tmds_mhz: int) -> bytes:
    max_tmds_code = min(255, max(1, math.ceil(float(max_tmds_mhz) / 5.0)))
    payload = bytes([0x03, 0x0C, 0x00, 0x10, 0x00, 0x00, max_tmds_code])
    return pack_data_block(0x03, payload)


def pack_hdmi_forum_vsdb(max_tmds_mhz: int) -> bytes:
    max_tmds_code = min(255, max(1, math.ceil(float(max_tmds_mhz) / 5.0)))
    # HDMI Forum VSDB. The short payload advertises version 1, max TMDS rate,
    # SCDC presence and scrambling support for rates above 340 MHz.
    payload = bytes([0xD8, 0x5D, 0xC4, 0x01, max_tmds_code, 0x03, 0x00])
    return pack_data_block(0x03, payload)


def build_base_block(name: str, vendor: str, product_id: int, serial: int, modes: list[VideoMode]) -> bytearray:
    native_mode = modes[0]
    block = bytearray(128)
    block[0:8] = b'\x00\xff\xff\xff\xff\xff\xff\x00'
    block[8:10] = encode_vendor_id(vendor)
    block[10:12] = int(product_id & 0xFFFF).to_bytes(2, 'little')
    block[12:16] = int(serial & 0xFFFFFFFF).to_bytes(4, 'little')
    block[16] = 1
    block[17] = 34
    block[18] = 1
    block[19] = 4
    block[20] = 0xB5
    block[21] = 0x00
    block[22] = 0x00
    block[23] = 0x78
    block[24:35] = b'\x0A\xCF\x74\xA3\x57\x4C\xB0\x23\x09\x48\x4C'
    block[35:38] = b'\x00\x00\x00'
    for i in range(38, 54):
        block[i] = 0x01
    width_mm = max(160, min(1600, round(native_mode.width * 0.265)))
    height_mm = max(90, min(900, round(native_mode.height * 0.265)))
    descriptors = [
        pack_dtd(native_mode, width_mm, height_mm),
        pack_monitor_name(name),
        pack_serial_text(f'{vendor}{serial:08X}'),
        pack_range_limits(modes),
    ]
    offset = 54
    for desc in descriptors:
        block[offset:offset + 18] = desc
        offset += 18
    block[126] = 1
    set_checksum(block)
    return block


def build_cta_extension(modes: list[VideoMode]) -> bytearray:
    block = bytearray(128)
    block[0] = 0x02
    block[1] = 0x03
    svds = []
    for idx, mode in enumerate(modes):
        if mode.vic is None:
            continue
        vic = int(mode.vic)
        if idx == 0:
            vic |= 0x80
        svds.append(vic)

    data = bytearray()
    if svds:
        data.extend(pack_data_block(0x02, bytes(svds)))

    max_tmds_mhz = max(340, max(math.ceil(mode.pixel_clock_khz / 1000.0) for mode in modes))
    data.extend(pack_hdmi_vsdb(max_tmds_mhz))
    if max_tmds_mhz > 340:
        data.extend(pack_hdmi_forum_vsdb(max_tmds_mhz))

    dtd_offset = 4 + len(data)
    if dtd_offset > 126:
        raise SystemExit(f'CTA data blocks too large: {len(data)} bytes')
    block[2] = dtd_offset
    block[3] = 0x00
    block[4:4 + len(data)] = data

    offset = dtd_offset
    for mode in modes:
        if offset + 18 > 127:
            break
        width_mm = max(160, min(1600, round(mode.width * 0.265)))
        height_mm = max(90, min(900, round(mode.height * 0.265)))
        block[offset:offset + 18] = pack_dtd(mode, width_mm, height_mm)
        offset += 18

    set_checksum(block)
    return block

def build_edid(name: str, vendor: str, product_id: int, serial: int, modes: list[VideoMode]) -> bytes:
    base = build_base_block(name, vendor, product_id, serial, modes)
    cta = build_cta_extension(modes)
    return bytes(base + cta)


def make_random_name(prefix: str = '') -> str:
    clean_prefix = ''.join(ch for ch in prefix.upper() if 'A' <= ch <= 'Z' or '0' <= ch <= '9')[:3] or 'OPI'
    suffix = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f'{clean_prefix}-{suffix}'


def make_random_vendor() -> str:
    return ''.join(random.choice(string.ascii_uppercase) for _ in range(3))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Generate a stable custom HDMI RX EDID binary')
    parser.add_argument('--name', default='OPI-COMPAT', help='Display name shown to the source device, ASCII recommended')
    parser.add_argument('--vendor', default='OPI', help='3-letter vendor code, e.g. OPI')
    parser.add_argument('--product-id', default='0x3588', help='16-bit product id, e.g. 0x3588')
    parser.add_argument('--serial', default='0x20260414', help='32-bit serial number')
    parser.add_argument('--random-identity', action='store_true', help='Randomize display name, vendor, product id and serial for this generated EDID')
    parser.add_argument('--native', required=True, help='Native/preferred mode, e.g. 1440p60')
    parser.add_argument('--add', action='append', default=[], help='Additional advertised mode, repeatable')
    parser.add_argument('--output', required=True, help='Output raw EDID binary path')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.random_identity:
        vendor = make_random_vendor()
        display_name = make_random_name(vendor)
        product_id = random.randint(0x0001, 0xFFFF)
        serial = random.randint(0x00000001, 0xFFFFFFFF)
    else:
        vendor = args.vendor.strip().upper()
        display_name = args.name.strip() or 'OPI-COMPAT'
        product_id = int(str(args.product_id), 0)
        serial = int(str(args.serial), 0)
    native_mode = parse_mode(args.native)
    seen = {native_mode.token}
    modes = [native_mode]
    for item in args.add:
        mode = parse_mode(item)
        if mode.token not in seen:
            seen.add(mode.token)
            modes.append(mode)
    edid = build_edid(
        name=display_name,
        vendor=vendor,
        product_id=product_id,
        serial=serial,
        modes=modes,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(edid)
    print(f'Wrote EDID: {output}')
    print(f'Display name: {display_name}')
    print(f'Vendor: {vendor}')
    print(f'Product ID: 0x{product_id:04X}')
    print(f'Serial: 0x{serial:08X}')
    print(f'Modes: {", ".join(mode.token for mode in modes)}')
    print(f'Bytes: {len(edid)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
