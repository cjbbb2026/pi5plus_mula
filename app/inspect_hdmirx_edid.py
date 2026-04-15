#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def decode_vendor_id(raw: bytes) -> str:
    if len(raw) != 2:
        return "???"
    value = (raw[0] << 8) | raw[1]
    chars = [
        chr(((value >> 10) & 0x1F) + 64),
        chr(((value >> 5) & 0x1F) + 64),
        chr((value & 0x1F) + 64),
    ]
    if any(ch < "A" or ch > "Z" for ch in chars):
        return "???"
    return "".join(chars)


def checksum_ok(block: bytes) -> bool:
    return len(block) == 128 and (sum(block) & 0xFF) == 0


def parse_descriptor_text(desc: bytes, tag: int) -> str | None:
    if len(desc) < 18:
        return None
    if desc[0:2] != b"\x00\x00" or desc[2] != 0x00 or desc[3] != tag:
        return None
    return bytes(desc[5:18]).decode("ascii", errors="ignore").strip(" \x00\n\r") or None


def parse_monitor_name(base_block: bytes) -> str | None:
    for offset in range(54, 126, 18):
        name = parse_descriptor_text(base_block[offset:offset + 18], 0xFC)
        if name:
            return name
    return None


def parse_range_limits(base_block: bytes) -> tuple[int, int, int, int, int] | None:
    for offset in range(54, 126, 18):
        desc = base_block[offset:offset + 18]
        if len(desc) < 18:
            continue
        if desc[0:2] != b"\x00\x00" or desc[2] != 0x00 or desc[3] != 0xFD:
            continue
        payload = desc[5:18]
        return payload[0], payload[1], payload[2], payload[3], payload[4] * 10
    return None


def parse_dtd(desc: bytes) -> dict[str, float | int] | None:
    if len(desc) < 18:
        return None
    pixel_clock_10khz = int(desc[0]) | (int(desc[1]) << 8)
    if pixel_clock_10khz <= 0:
        return None

    h_active = int(desc[2]) | (((int(desc[4]) >> 4) & 0x0F) << 8)
    h_blank = int(desc[3]) | ((int(desc[4]) & 0x0F) << 8)
    v_active = int(desc[5]) | (((int(desc[7]) >> 4) & 0x0F) << 8)
    v_blank = int(desc[6]) | ((int(desc[7]) & 0x0F) << 8)
    h_total = h_active + h_blank
    v_total = v_active + v_blank
    pixel_clock_mhz = float(pixel_clock_10khz) / 100.0
    refresh_hz = 0.0
    if h_total > 0 and v_total > 0:
        refresh_hz = (float(pixel_clock_10khz) * 10000.0) / float(h_total * v_total)
    return {
        "width": h_active,
        "height": v_active,
        "refresh_hz": refresh_hz,
        "pixel_clock_mhz": pixel_clock_mhz,
        "h_total": h_total,
        "v_total": v_total,
    }


def collect_base_dtds(base_block: bytes) -> list[dict[str, float | int]]:
    modes: list[dict[str, float | int]] = []
    for offset in range(54, 126, 18):
        parsed = parse_dtd(base_block[offset:offset + 18])
        if parsed is not None:
            modes.append(parsed)
    return modes


def collect_cta_data_blocks(cta_block: bytes) -> list[tuple[int, bytes]]:
    if len(cta_block) != 128 or cta_block[0] != 0x02:
        return []
    dtd_offset = cta_block[2]
    end = dtd_offset if 4 <= dtd_offset <= 127 else 4
    blocks: list[tuple[int, bytes]] = []
    idx = 4
    while idx < end:
        header = cta_block[idx]
        tag = (header >> 5) & 0x07
        length = header & 0x1F
        payload = bytes(cta_block[idx + 1: idx + 1 + length])
        blocks.append((tag, payload))
        idx += 1 + length
    return blocks


def collect_cta_dtds(cta_block: bytes) -> list[dict[str, float | int]]:
    if len(cta_block) != 128 or cta_block[0] != 0x02:
        return []
    dtd_offset = cta_block[2]
    if not 4 <= dtd_offset <= 126:
        return []
    modes: list[dict[str, float | int]] = []
    offset = dtd_offset
    while offset + 18 <= 126:
        parsed = parse_dtd(cta_block[offset:offset + 18])
        if parsed is None:
            break
        modes.append(parsed)
        offset += 18
    return modes


def collect_svd_vics(data_blocks: list[tuple[int, bytes]]) -> list[str]:
    vics: list[str] = []
    for tag, payload in data_blocks:
        if tag != 0x02:
            continue
        for value in payload:
            native = bool(value & 0x80)
            vic = int(value & 0x7F)
            vics.append(f"{vic}{'*' if native else ''}")
    return vics


def get_hdmi_vsdb_max_tmds(data_blocks: list[tuple[int, bytes]]) -> int | None:
    for tag, payload in data_blocks:
        if tag == 0x03 and payload[:3] == bytes([0x03, 0x0C, 0x00]) and len(payload) >= 7:
            return int(payload[6]) * 5
    return None


def get_hdmi_forum_vsdb_info(data_blocks: list[tuple[int, bytes]]) -> tuple[int | None, bool, bool] | None:
    for tag, payload in data_blocks:
        if tag == 0x03 and payload[:3] == bytes([0xD8, 0x5D, 0xC4]):
            max_tmds = int(payload[4]) * 5 if len(payload) >= 5 else None
            flags = int(payload[5]) if len(payload) >= 6 else 0
            scdc_present = bool(flags & 0x01)
            scrambling = bool(flags & 0x02)
            return max_tmds, scdc_present, scrambling
    return None


def has_hdmi_vsdb(data_blocks: list[tuple[int, bytes]]) -> bool:
    for tag, payload in data_blocks:
        if tag == 0x03 and payload[:3] == bytes([0x03, 0x0C, 0x00]):
            return True
    return False


def has_hdmi_forum_vsdb(data_blocks: list[tuple[int, bytes]]) -> bool:
    for tag, payload in data_blocks:
        if tag == 0x03 and payload[:3] == bytes([0xD8, 0x5D, 0xC4]):
            return True
    return False


def has_svd_block(data_blocks: list[tuple[int, bytes]]) -> bool:
    return any(tag == 0x02 and len(payload) > 0 for tag, payload in data_blocks)


def format_mode(mode: dict[str, float | int]) -> str:
    return (
        f"{int(mode['width'])}x{int(mode['height'])}"
        f"@{float(mode['refresh_hz']):.2f}Hz"
        f" pclk={float(mode['pixel_clock_mhz']):.2f}MHz"
        f" total={int(mode['h_total'])}x{int(mode['v_total'])}"
    )


def inspect_edid(data: bytes) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    summary: list[str] = []

    if len(data) < 128 or len(data) % 128 != 0:
        errors.append(f"EDID 长度不合法: {len(data)} 字节")
        return errors, warnings, summary

    block_count = len(data) // 128
    for idx in range(block_count):
        block = data[idx * 128:(idx + 1) * 128]
        if not checksum_ok(block):
            errors.append(f"Block {idx} 校验和错误")

    base = data[:128]
    if base[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        errors.append("Base block 头部不是标准 EDID header")
    else:
        vendor = decode_vendor_id(base[8:10])
        product_id = int.from_bytes(base[10:12], "little")
        serial = int.from_bytes(base[12:16], "little")
        name = parse_monitor_name(base) or "(未声明)"
        summary.append(f"显示器名称: {name}")
        summary.append(f"厂商/产品/序列号: {vendor} / 0x{product_id:04X} / 0x{serial:08X}")

    extension_count = base[126]
    if extension_count != block_count - 1:
        warnings.append(f"Base block 标记扩展数={extension_count}，实际块数={block_count - 1}")

    range_limits = parse_range_limits(base)
    max_pixel_clock_mhz = 0
    if range_limits is None:
        warnings.append("未找到 range limits 描述块 (0xFD)")
    else:
        min_v, max_v, min_h, max_h, max_pclk = range_limits
        max_pixel_clock_mhz = int(max_pclk)
        summary.append(f"Range limits: 垂直 {min_v}-{max_v} Hz，水平 {min_h}-{max_h} kHz，最大像素时钟 {max_pclk} MHz")
        if min_v > max_v:
            errors.append(f"垂直频率范围错误: {min_v}-{max_v} Hz")
        if min_h > max_h:
            errors.append(f"水平频率范围错误: {min_h}-{max_h} kHz")
        if max_pclk <= 0:
            errors.append("最大像素时钟无效")

    base_dtds = collect_base_dtds(base)
    if base_dtds:
        summary.append("Base DTD:")
        for idx, mode in enumerate(base_dtds, start=1):
            summary.append(f"  {idx}. {format_mode(mode)}")
    else:
        warnings.append("Base block 中没有详细时序 DTD")

    if block_count >= 2:
        cta = data[128:256]
        if cta[0] != 0x02:
            warnings.append("扩展块 1 不是 CTA-861")
        else:
            data_blocks = collect_cta_data_blocks(cta)
            summary.append(f"CTA-861: revision={cta[1]}，DTD offset={cta[2]}，data blocks={len(data_blocks)}")
            vics = collect_svd_vics(data_blocks)
            if vics:
                summary.append(f"SVD VIC: {', '.join(vics)} (* 表示 native/preferred)")
            if not has_svd_block(data_blocks):
                warnings.append("CTA 扩展中没有 SVD 视频数据块，部分显卡兼容性可能较差")
            hdmi_tmds = get_hdmi_vsdb_max_tmds(data_blocks)
            if hdmi_tmds is None:
                warnings.append("CTA 扩展中缺少 HDMI VSDB")
            else:
                summary.append(f"HDMI VSDB: 存在，最大 TMDS {hdmi_tmds} MHz")
            forum_info = get_hdmi_forum_vsdb_info(data_blocks)
            if max_pixel_clock_mhz > 340 and not has_hdmi_forum_vsdb(data_blocks):
                warnings.append("CTA 扩展中缺少 HDMI Forum VSDB，高带宽模式兼容性可能较差")
            elif forum_info is not None:
                forum_tmds, scdc_present, scrambling = forum_info
                summary.append(
                    "HDMI Forum VSDB: 存在，"
                    f"最大 TMDS {forum_tmds if forum_tmds is not None else '未知'} MHz，"
                    f"SCDC={'是' if scdc_present else '否'}，"
                    f"Scrambling={'是' if scrambling else '否'}"
                )
            cta_dtds = collect_cta_dtds(cta)
            if cta_dtds:
                summary.append("CTA DTD:")
                for idx, mode in enumerate(cta_dtds, start=1):
                    summary.append(f"  {idx}. {format_mode(mode)}")
    else:
        warnings.append("没有 CTA 扩展块，HDMI 源端兼容性可能较差")

    return errors, warnings, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a generated HDMI RX EDID binary")
    parser.add_argument("edid", help="Path to raw EDID binary")
    args = parser.parse_args()

    path = Path(args.edid).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"EDID 文件不存在: {path}")

    data = path.read_bytes()
    errors, warnings, summary = inspect_edid(data)

    print(f"EDID: {path}")
    print(f"Bytes: {len(data)}")
    print(f"Blocks: {len(data) // 128 if len(data) % 128 == 0 else 'invalid'}")
    if summary:
        print("摘要:")
        for item in summary:
            print(f"  {item}")

    if not errors and not warnings:
        print("检查结果: 通过")
        return 0

    if errors:
        print("错误:")
        for item in errors:
            print(f"  - {item}")

    if warnings:
        print("警告:")
        for item in warnings:
            print(f"  - {item}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
