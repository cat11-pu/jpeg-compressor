#!/usr/bin/env python3
import json
import struct
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/minimal_cases"


def sof(width, height, components):
    payload = bytes([8]) + struct.pack(">HHB", height, width, len(components))
    payload += b"".join(bytes([component, sampling, quant]) for component, sampling, quant in components)
    return b"\xff\xc0" + struct.pack(">H", len(payload) + 2) + payload


def sos(component_pairs, spectral=True):
    payload = bytes([len(component_pairs)]) + b"".join(bytes(pair) for pair in component_pairs)
    if spectral:
        payload += bytes([0, 63, 0])
    return b"\xff\xda" + struct.pack(">H", len(payload) + 2) + payload


def dqt_one():
    return b"\xff\xdb\x00\x43\x00" + bytes([1]) * 64


def dht(index, symbol):
    counts = bytes([1]) + bytes([0]) * 15
    return b"\xff\xc4\x00\x14" + bytes([index]) + counts + bytes([symbol])


def valid_header(width, height):
    jfif = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = sof(width, height, [(1, 0x22, 0), (2, 0x11, 1), (3, 0x11, 1)])
    return (b"\xff\xd8" + jfif + dqt_one() + dqt_one().replace(b"\x00\x43\x00", b"\x00\x43\x01") +
            dht(0x00, 0) + dht(0x10, 0) + frame + sos([(1, 0), (2, 0), (3, 0)]))


def minimal_frame():
    return sof(8, 8, [(1, 0x11, 0), (2, 0x11, 0), (3, 0x11, 0)])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cases = []

    duplicate = b"\xff\xd8" + minimal_frame() + sos([(1, 0)] * 4)
    (OUT / "duplicate_sos_component.jpg").write_bytes(duplicate)
    cases.append({
        "file": "duplicate_sos_component.jpg",
        "title": "SOS 同一组件重复 4 次",
        "kind": "malformed",
        "expected": "JPGD_BAD_SOS_COMP_ID",
        "result": "拒绝并报告 SOS 头部内偏移",
    })

    dqt = b"\xff\xd8\xff\xdb\x00\x03\x04"
    (OUT / "dqt_table_index_4.jpg").write_bytes(dqt)
    cases.append({
        "file": "dqt_table_index_4.jpg",
        "title": "DQT 表号 4",
        "kind": "malformed",
        "expected": "JPGD_BAD_DQT_TABLE",
        "result": "拒绝并报告 DQT 表号偏移",
    })

    sos_oob = b"\xff\xd8" + minimal_frame() + sos([(1, 0x80), (2, 0), (3, 0)])
    (OUT / "sos_dc_table_index_8.jpg").write_bytes(sos_oob)
    cases.append({
        "file": "sos_dc_table_index_8.jpg",
        "title": "SOS DC 表号 8",
        "kind": "malformed",
        "expected": "JPGD_DECODE_ERROR",
        "result": "拒绝并报告 SOS 表选择器偏移",
    })

    huge = (b"\xff\xd8" + dqt_one() + dht(0x00, 0) + dht(0x10, 0) +
            sof(32768, 32768, [(1, 0x11, 0), (2, 0x11, 0), (3, 0x11, 0)]) +
            sos([(1, 0), (2, 0), (3, 0)]))
    (OUT / "dimensions_32768x32768.jpg").write_bytes(huge)
    cases.append({
        "file": "dimensions_32768x32768.jpg",
        "title": "32768×32768 输出尺寸乘法溢出",
        "kind": "malformed",
        "expected": "wrapper null",
        "result": "类接口受限读取；包装 API 安全拒绝，不分配回绕缓冲区",
    })

    (OUT / "truncated_after_soi.jpg").write_bytes(b"\xff\xd8")
    cases.append({
        "file": "truncated_after_soi.jpg",
        "title": "仅保留 SOI 的截断流",
        "kind": "malformed",
        "expected": "JPGD_UNSUPPORTED_MARKER",
        "result": "拒绝并报告 EOF 偏移",
    })

    import io
    tiny_buffer = io.BytesIO()
    Image.frombytes("RGB", (1, 1), bytes([54, 41, 7])).save(
        tiny_buffer, "JPEG", quality=75, subsampling=2
    )
    (OUT / "valid_1x1_420.jpg").write_bytes(tiny_buffer.getvalue())
    cases.append({
        "file": "valid_1x1_420.jpg",
        "title": "合法 1×1 4:2:0",
        "kind": "valid",
        "expected": "decoded",
        "result": "完整解码，线性色度上采样不再产生负索引",
    })

    (OUT / "manifest.json").write_text(json.dumps(cases, indent=2) + "\n")
    print(json.dumps(cases, indent=2))


if __name__ == "__main__":
    main()
