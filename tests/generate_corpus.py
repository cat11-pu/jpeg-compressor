#!/usr/bin/env python3
import argparse
import csv
import json
import os
import random
import struct
from pathlib import Path

from PIL import Image


STANDALONE = {0x01, *range(0xD0, 0xD8), 0xD8, 0xD9}


def parse_segments(data):
    if len(data) < 2 or data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    segments = []
    pos = 2
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker_start = pos
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in STANDALONE:
            segments.append({
                "marker": marker,
                "start": marker_start,
                "length_offset": None,
                "payload": pos,
                "end": pos,
                "entropy_start": None,
                "entropy_end": None,
            })
            if marker == 0xD9:
                break
            continue
        if pos + 2 > len(data):
            break
        length = struct.unpack_from(">H", data, pos)[0]
        segment_end = pos + length
        if length < 2 or segment_end > len(data):
            break
        entropy_start = segment_end if marker == 0xDA else None
        entropy_end = None
        if marker == 0xDA:
            scan = segment_end
            while scan + 1 < len(data):
                if data[scan] != 0xFF:
                    scan += 1
                    continue
                nxt = scan + 1
                while nxt < len(data) and data[nxt] == 0xFF:
                    nxt += 1
                if nxt >= len(data):
                    entropy_end = len(data)
                    break
                if data[nxt] == 0x00 or 0xD0 <= data[nxt] <= 0xD7:
                    scan = nxt + 1
                    continue
                entropy_end = scan
                break
            if entropy_end is None:
                entropy_end = len(data)
        segments.append({
            "marker": marker,
            "start": marker_start,
            "length_offset": pos,
            "payload": pos + 2,
            "end": segment_end,
            "entropy_start": entropy_start,
            "entropy_end": entropy_end,
        })
        pos = segment_end if entropy_end is None else entropy_end
    return segments


def first_segment(segments, marker):
    return next((segment for segment in segments if segment["marker"] == marker), None)


def all_segments(segments, marker):
    return [segment for segment in segments if segment["marker"] == marker]


def make_valid_images(root, count=500):
    sizes = [
        (2, 3), (3, 2), (4, 4), (7, 5), (8, 8), (9, 7), (15, 16),
        (16, 15), (17, 19), (31, 33), (32, 32), (33, 31), (63, 65),
        (64, 64), (65, 63), (127, 129), (128, 128), (129, 127),
        (191, 97), (255, 257), (256, 256),
    ]
    qualities = [25, 40, 55, 70, 85, 92, 97, 100]
    modes = ["L", "RGB444", "RGB422", "RGB420", "PROG444", "PROG422", "PROG420"]
    manifest = []
    rng = random.Random(3571)
    for index in range(count):
        width, height = sizes[(index * 7 + index // len(qualities)) % len(sizes)]
        mode_spec = modes[(index * 3 + index // len(sizes)) % len(modes)]
        quality = qualities[(index * 5 + index // len(modes)) % len(qualities)]
        channels = 1 if mode_spec == "L" else 3
        pixels = rng.randbytes(width * height * channels)
        image_mode = "L" if channels == 1 else "RGB"
        image = Image.frombytes(image_mode, (width, height), pixels)
        save_args = {"quality": quality, "optimize": False}
        if channels == 3:
            subsampling = 0 if mode_spec.endswith("444") else 1 if mode_spec.endswith("422") else 2
            save_args["subsampling"] = subsampling
        if mode_spec.startswith("PROG"):
            save_args["progressive"] = True
        if index % 5 == 0:
            save_args["restart_marker_blocks"] = 1 + index % 7
        path = root / f"v{index:04d}_{image_mode}_{width}x{height}.jpg"
        try:
            image.save(path, "JPEG", **save_args)
        except TypeError:
            save_args.pop("restart_marker_blocks", None)
            image.save(path, "JPEG", **save_args)
        manifest.append({
            "id": f"valid_{index:04d}",
            "path": path.name,
            "mode": image_mode,
            "width": width,
            "height": height,
            "category": mode_spec,
            "quality": quality,
        })
    return manifest


def write_case(root, name, data, manifest, case_id, category, seed_name, mutation):
    path = root / name
    path.write_bytes(data)
    manifest.append({
        "id": case_id,
        "path": path.name,
        "category": category,
        "seed": seed_name,
        "mutation": mutation,
        "size": len(data),
    })


def mutate_truncation(seeds, root, manifest, start_id):
    case = 0
    for seed_name, data, segments in seeds:
        points = {1, 2, 3, len(data) - 1}
        for segment in segments:
            for key in ("start", "payload", "end", "entropy_start", "entropy_end"):
                value = segment[key]
                if value is not None:
                    points.update((value - 1, value, value + 1))
        for step in range(1, 51):
            points.add(max(1, min(len(data) - 1, len(data) * step // 51)))
        ordered = sorted(point for point in points if 0 < point < len(data))
        stride = max(1, len(ordered) // 50)
        for j in range(50):
            cut = ordered[min(len(ordered) - 1, j * stride)]
            case_id = f"trunc_{start_id + case:04d}"
            write_case(root, case_id + ".jpg", data[:cut], manifest, case_id,
                       "truncation", seed_name, f"cut={cut}")
            case += 1
    return case


def mutate_tables(seeds, root, manifest, start_id):
    case = 0
    kinds = ["dqt_table", "dht_index", "dht_count", "sof_tq", "sos_dc", "sos_ac"]
    for seed_name, data, segments in seeds:
        for j in range(50):
            kind = kinds[(case + j) % len(kinds)]
            mutated = bytearray(data)
            detail = kind
            if kind == "dqt_table":
                segment = first_segment(segments, 0xDB)
                position = segment["payload"]
                mutated[position] = (mutated[position] & 0xF0) | (4 + j % 12)
                detail += f"@{position}={mutated[position]:02x}"
            elif kind == "dht_index":
                segment = first_segment(segments, 0xC4)
                position = segment["payload"]
                mutated[position] = (mutated[position] & 0xF0) | (8 + j % 8)
                detail += f"@{position}={mutated[position]:02x}"
            elif kind == "dht_count":
                segment = first_segment(segments, 0xC4)
                position = segment["payload"] + 1 + (j % 16)
                mutated[position] = 255
                detail += f"@{position}={mutated[position]:02x}"
            elif kind == "sof_tq":
                segment = first_segment(segments, next(m for m in (0xC0, 0xC1, 0xC2) if first_segment(segments, m)))
                component_count = mutated[segment["payload"] + 5]
                position = segment["payload"] + 8 + (j % component_count) * 3
                mutated[position] = 4 + j % 12
                detail += f"@{position}={mutated[position]:02x}"
            elif kind == "sos_dc":
                segment = first_segment(segments, 0xDA)
                component_count = mutated[segment["payload"]]
                pair = segment["payload"] + 2 + (j % component_count) * 2
                mutated[pair] = (8 + j % 8) << 4 | (mutated[pair] & 0x0F)
                detail += f"@{pair}={mutated[pair]:02x}"
            else:
                segment = first_segment(segments, 0xDA)
                component_count = mutated[segment["payload"]]
                pair = segment["payload"] + 2 + (j % component_count) * 2
                mutated[pair] = (mutated[pair] & 0xF0) | (4 + j % 4)
                detail += f"@{pair}={mutated[pair]:02x}"
            case_id = f"table_{start_id + case:04d}"
            write_case(root, case_id + ".jpg", bytes(mutated), manifest, case_id,
                       "table_entry_oob", seed_name, detail)
            case += 1
    return case


def duplicate_sos_components(data, segments, duplicate_count):
    sos = first_segment(segments, 0xDA)
    payload = sos["payload"]
    count = data[payload]
    pairs = [bytes(data[payload + 1 + 2 * i:payload + 3 + 2 * i]) for i in range(count)]
    new_pairs = [pairs[0]] * duplicate_count
    new_count = len(new_pairs)
    length = 2 + 1 + 2 * new_count + 3
    header = b"\xff\xda" + struct.pack(">H", length) + bytes([new_count]) + b"".join(new_pairs)
    new_spectral = bytes(data[sos["end"] - 3:sos["end"]])
    return data[:sos["start"]] + header + new_spectral + data[sos["end"]:]


def duplicate_scan(data, segments, copies):
    sos = first_segment(segments, 0xDA)
    eoi = first_segment(segments, 0xD9)
    scan = bytes(data[sos["start"]:sos["entropy_end"]])
    return data[:sos["entropy_end"]] + scan * copies + data[sos["entropy_end"]:]


def mutate_duplicate_scans(seeds, scan_seeds, root, manifest, start_id):
    case = 0
    for seed_index, (seed_name, data, segments) in enumerate(seeds):
        for j in range(25):
            duplicated = duplicate_sos_components(data, segments, 2 + (j % 3))
            case_id = f"dupcomp_{start_id + case:04d}"
            write_case(root, case_id + ".jpg", duplicated, manifest, case_id,
                       "duplicate_scan", seed_name, f"duplicate_component_scan={2 + (j % 3)}")
            case += 1
    for seed_index, (seed_name, data, segments) in enumerate(scan_seeds):
        for j in range(25):
            duplicated = duplicate_scan(data, segments, 1 + (j % 25))
            case_id = f"dupscan_{start_id + case:04d}"
            write_case(root, case_id + ".jpg", duplicated, manifest, case_id,
                       "duplicate_scan", seed_name, f"repeat_scan={1 + (j % 25)}")
            case += 1
    return case


def mutate_oversized(seeds, root, manifest, start_id):
    case = 0
    dimensions = [
        (32768, 32768), (65535, 65535), (32769, 1), (1, 32769),
        (32768, 1), (1, 32768), (32768, 32767), (32767, 32768),
        (40000, 2), (2, 40000), (0, 0),
    ]
    for seed_name, data, segments in seeds:
        sof = first_segment(segments, next(m for m in (0xC0, 0xC1, 0xC2) if first_segment(segments, m)))
        height_offset = sof["payload"] + 1
        width_offset = sof["payload"] + 3
        for j in range(50):
            width, height = dimensions[(case + j) % len(dimensions)]
            mutated = bytearray(data)
            struct.pack_into(">H", mutated, width_offset, width)
            struct.pack_into(">H", mutated, height_offset, height)
            case_id = f"huge_{start_id + case:04d}"
            write_case(root, case_id + ".jpg", bytes(mutated), manifest, case_id,
                       "oversized_dimensions", seed_name, f"{width}x{height}")
            case += 1
    return case


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.output
    valid_root = root / "valid"
    malformed_root = root / "malformed"
    valid_root.mkdir(parents=True, exist_ok=True)
    malformed_root.mkdir(parents=True, exist_ok=True)

    valid_manifest = make_valid_images(valid_root)
    with (root / "valid_manifest.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=valid_manifest[0].keys())
        writer.writeheader()
        writer.writerows(valid_manifest)

    seed_names = [0, 1, 2, 4, 5, 6, 9, 10, 13, 15, 19, 24, 31, 42, 55]
    seeds = []
    for index in seed_names:
        path = valid_root / valid_manifest[index]["path"]
        data = path.read_bytes()
        seeds.append((path.name, data, parse_segments(data)))
    scan_seeds = []
    for item in valid_manifest:
        if item["category"].startswith("PROG"):
            path = valid_root / item["path"]
            data = path.read_bytes()
            scan_seeds.append((path.name, data, parse_segments(data)))
            if len(scan_seeds) == 15:
                break

    malformed_manifest = []
    assert mutate_truncation(seeds, malformed_root, malformed_manifest, 0) == 750
    assert mutate_tables(seeds, malformed_root, malformed_manifest, 0) == 750
    assert mutate_duplicate_scans(seeds, scan_seeds, malformed_root, malformed_manifest, 0) == 750
    assert mutate_oversized(seeds, malformed_root, malformed_manifest, 0) == 750
    with (root / "malformed_manifest.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=malformed_manifest[0].keys())
        writer.writeheader()
        writer.writerows(malformed_manifest)

    summary = {
        "valid": len(valid_manifest),
        "malformed": len(malformed_manifest),
        "categories": {},
    }
    for item in malformed_manifest:
        summary["categories"][item["category"]] = summary["categories"].get(item["category"], 0) + 1
    (root / "corpus_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
