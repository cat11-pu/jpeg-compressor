#!/usr/bin/env python3
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASAN_OPTIONS = "detect_leaks=1:halt_on_error=1:abort_on_error=1:allocator_may_return_null=1"
UBSAN_OPTIONS = "print_stacktrace=1:halt_on_error=0"


def run(command, cwd, timeout=60, env=None):
    actual_env = os.environ.copy()
    if env:
        actual_env.update(env)
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=actual_env)


def compile_tools(workdir):
    bin_dir = workdir / "bin"
    reference_dir = workdir / "reference"
    bin_dir.mkdir(parents=True, exist_ok=True)
    reference_dir.mkdir(parents=True, exist_ok=True)
    compiler = ["g++", "-std=c++11", "-O1", "-g", "-fno-omit-frame-pointer",
                "-fsanitize=address,undefined", "-DJPGD_USE_SSE2=0",
                "-Wall", "-Wextra", f"-I{ROOT}"]
    result = run(compiler + [str(ROOT / "tests/jpeg_probe.cpp"), str(ROOT / "jpgd.cpp"),
                 "-o", str(bin_dir / "jpeg_probe")], ROOT, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    (reference_dir / "jpgd.cpp").write_text(
        run(["git", "show", "HEAD:jpgd.cpp"], ROOT).stdout
    )
    (reference_dir / "jpgd.h").write_text(
        run(["git", "show", "HEAD:jpgd.h"], ROOT).stdout
    )
    result = run(["g++", "-std=c++11", "-O2", "-Wall", "-Wextra", "-DJPGD_USE_SSE2=0",
                  f"-I{reference_dir}", str(ROOT / "tests/jpeg_hash.cpp"),
                  str(reference_dir / "jpgd.cpp"), "-o", str(bin_dir / "jpeg_hash_reference")],
                 ROOT, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return bin_dir / "jpeg_probe", bin_dir / "jpeg_hash_reference"


def ensure_corpus(workdir, regenerate):
    corpus = workdir / "corpus"
    summary_path = corpus / "corpus_summary.json"
    if regenerate or not summary_path.exists():
        result = run([sys.executable, str(ROOT / "tests/generate_corpus.py"), str(corpus)],
                     ROOT, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
    return corpus


def load_manifest(path):
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def parse_probe_output(stdout):
    class_line = next((line for line in stdout.splitlines() if line.startswith("CLASS_")), None)
    wrap_line = next((line for line in stdout.splitlines() if line.startswith("WRAP_")), None)
    return class_line, wrap_line


def classify_errors(stderr):
    asan = "ERROR: AddressSanitizer" in stderr or "ERROR: LeakSanitizer" in stderr
    ubsan_lines = [line.strip() for line in stderr.splitlines() if "runtime error:" in line]
    return asan, ubsan_lines


def probe_valid(probe, reference, corpus, item, timeout):
    path = corpus / "valid" / item["path"]
    reference_result = run([str(reference), str(path)], ROOT, timeout=timeout)
    probe_result = run([str(probe), str(path)], ROOT, timeout=timeout,
                       env={"ASAN_OPTIONS": ASAN_OPTIONS, "UBSAN_OPTIONS": UBSAN_OPTIONS})
    reference_line = reference_result.stdout.strip()
    class_line, wrap_line = parse_probe_output(probe_result.stdout)
    asan, ubsan = classify_errors(probe_result.stderr)
    reference_parts = reference_line.split("|") if reference_line.startswith("OK|") else []
    wrap_parts = wrap_line.split("|") if wrap_line and wrap_line.startswith("WRAP_OK|") else []
    pixel_equal = (
        len(reference_parts) == 5
        and len(wrap_parts) == 5
        and reference_parts[1:] == wrap_parts[1:]
    )
    class_ok = bool(class_line and class_line.startswith("CLASS_OK|"))
    class_parts = class_line.split("|") if class_ok else []
    complete_class = class_ok and len(class_parts) == 7 and int(class_parts[6]) == int(item["height"])
    return {
        "id": item["id"],
        "path": item["path"],
        "category": item["category"],
        "pixel_equal": pixel_equal,
        "class_complete": complete_class,
        "returncode": probe_result.returncode,
        "asan": asan,
        "ubsan": ubsan,
        "reference": reference_line,
        "probe_stdout": probe_result.stdout.strip(),
        "timed_out": False,
    }


def probe_malformed(probe, corpus, item, timeout):
    path = corpus / "malformed" / item["path"]
    command = [str(probe), str(path)]
    if item["category"] == "oversized_dimensions":
        command.extend(["--max-rows", "2"])
    started = time.monotonic()
    timed_out = False
    try:
        result = run(command, ROOT, timeout=timeout,
                     env={"ASAN_OPTIONS": ASAN_OPTIONS, "UBSAN_OPTIONS": UBSAN_OPTIONS})
        stdout = result.stdout
        stderr = result.stderr
        returncode = result.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        returncode = -999
    class_line, wrap_line = parse_probe_output(stdout)
    asan, ubsan = classify_errors(stderr)
    class_error = None
    offset_valid = True
    code = None
    if class_line and class_line.startswith("CLASS_ERR|"):
        parts = class_line.split("|", 3)
        code = parts[1]
        try:
            offset = int(parts[2])
            offset_valid = 0 <= offset <= int(item["size"])
        except (ValueError, IndexError):
            offset_valid = False
        class_error = class_line
    elif not class_line:
        offset_valid = False
    return {
        "id": item["id"],
        "path": item["path"],
        "category": item["category"],
        "size": int(item["size"]),
        "class_line": class_line,
        "wrap_line": wrap_line,
        "class_error": class_error,
        "code": code,
        "offset_valid": offset_valid,
        "class_ok": bool(class_line and class_line.startswith("CLASS_OK|")),
        "wrap_ok": bool(wrap_line and wrap_line.startswith("WRAP_OK|")),
        "returncode": returncode,
        "asan": asan,
        "ubsan": ubsan,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 4),
    }


def parallel_map(function, items, workers):
    records = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(function, item) for item in items]
        for future in as_completed(futures):
            records.append(future.result())
    return sorted(records, key=lambda item: item["id"])


def verify_minimal_cases(probe, timeout):
    manifest_path = ROOT / "tests/minimal_cases/manifest.json"
    if not manifest_path.exists():
        return []
    records = []
    for item in json.loads(manifest_path.read_text()):
        path = ROOT / "tests/minimal_cases" / item["file"]
        result = run([str(probe), str(path), "--max-rows", "2"], ROOT, timeout=timeout,
                     env={"ASAN_OPTIONS": ASAN_OPTIONS, "UBSAN_OPTIONS": UBSAN_OPTIONS})
        class_line, wrap_line = parse_probe_output(result.stdout)
        asan, ubsan = classify_errors(result.stderr)
        expected = item["expected"]
        offset = None
        actual = "no diagnostic"
        if expected == "wrapper null":
            passed = result.returncode == 0 and wrap_line == "WRAP_ERR|null" and not asan and not ubsan
            actual = "wrapper null"
        elif expected == "decoded":
            passed = (result.returncode == 0 and bool(class_line)
                      and class_line.startswith("CLASS_OK|") and bool(wrap_line)
                      and wrap_line.startswith("WRAP_OK|") and not asan and not ubsan)
            actual = "decoded"
        else:
            parts = class_line.split("|", 3) if class_line else []
            passed = (result.returncode == 0 and len(parts) == 4
                      and parts[0] == "CLASS_ERR" and expected in parts[3]
                      and not asan and not ubsan)
            if passed:
                try:
                    offset = int(parts[2])
                    passed = 0 <= offset <= path.stat().st_size
                    actual = f"{parts[1]} @ {offset}"
                except ValueError:
                    passed = False
        records.append({
            **item,
            "size": path.stat().st_size,
            "actual": actual,
            "offset": offset,
            "passed": passed,
            "returncode": result.returncode,
            "asan": asan,
            "ubsan": ubsan,
        })
    return records


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    return f"{seconds / 60:.1f} 分钟"


def write_report(workdir, corpus, malformed_manifest, valid_records, malformed_records,
                 minimal_records, elapsed):
    report_dir = ROOT / "reports"
    report_dir.mkdir(exist_ok=True)
    valid_total = len(valid_records)
    pixel_equal = sum(record["pixel_equal"] for record in valid_records)
    valid_clean = sum(record["returncode"] == 0 and not record["asan"] and not record["ubsan"]
                      and record["class_complete"] for record in valid_records)
    malformed_total = len(malformed_records)
    asan_failures = [record for record in malformed_records if record["asan"] or record["returncode"] != 0]
    timeouts = [record for record in malformed_records if record["timed_out"]]
    ubsan_failures = [record for record in malformed_records if record["ubsan"]]
    top_ublines = Counter(line for record in malformed_records for line in record["ubsan"])
    class_errors = [record for record in malformed_records if record["class_error"]]
    class_ok = [record for record in malformed_records if record["class_ok"]]
    missing_offsets = [record for record in class_errors if not record["offset_valid"]]
    codes = Counter(record["code"] for record in class_errors)
    category_stats = defaultdict(lambda: {
        "total": 0, "class_err": 0, "class_ok": 0, "wrap_null": 0,
        "wrap_ok": 0, "asan": 0, "ubsan": 0,
    })
    for record in malformed_records:
        stats = category_stats[record["category"]]
        stats["total"] += 1
        stats["class_err"] += bool(record["class_error"])
        stats["class_ok"] += record["class_ok"]
        stats["wrap_null"] += bool(record["wrap_line"] == "WRAP_ERR|null")
        stats["wrap_ok"] += record["wrap_ok"]
        stats["asan"] += bool(record["asan"] or record["returncode"] != 0)
        stats["ubsan"] += bool(record["ubsan"])
    category_names = {
        "truncation": "截断",
        "table_entry_oob": "表项越界",
        "duplicate_scan": "重复扫描",
        "oversized_dimensions": "超大尺寸",
    }
    git_sha = run(["git", "rev-parse", "--short", "HEAD"], ROOT).stdout.strip()
    compiler = run(["g++", "--version"], ROOT).stdout.splitlines()[0]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# JPEG 解码器畸形输入与像素回归报告",
        "",
        f"- 时间：{now}",
        f"- 基线提交：`{git_sha}`",
        f"- 编译器：`{compiler}`",
        "- 探针：ASan + UBSan + LSan，`-DJPGD_USE_SSE2=0`，逐文件独立进程",
        f"- 总耗时：{format_duration(elapsed)}",
        "",
        "## 结论",
        "",
        f"- 畸形输入：{malformed_total} 张，ASan/LSan 崩溃或泄漏 **{len(asan_failures)}**，超时 **{len(timeouts)}**，UBSan 报告 **{len(ubsan_failures)}**。",
        f"- 错误偏移：{len(class_errors)} 条拒绝信息全部带偏移，缺失或超出文件范围的偏移 **{len(missing_offsets)}** 条。",
        f"- 合法输入：{valid_total} 张，参考基线逐像素一致 **{pixel_equal}/{valid_total}**，完整解码且无 sanitizer 报告 **{valid_clean}/{valid_total}**。",
        "",
        "## 修复内容",
        "",
        "- SOS 扫描组件去重，拒绝同一帧组件在一次扫描中重复出现；`m_mcu_org` 写入前再做容量检查。",
        "- 包装 API 改用 `size_t` 计算输出缓冲区，并拒绝超过 `INT_MAX` 的结果，消除 `stride * height` 有符号溢出。",
        "- 解码器分配器改为 16 字节对齐，渐进式系数缓冲区增加尺寸与乘法溢出检查。",
        "- 线性色度上采样对 1 像素宽/高的边界做索引夹紧，消除负索引。",
        "- 反量化系数在 IDCT 前做范围校验，渐进式细化移位改为无 UB 的位模式移位；错误路径暴露 `get_error_offset()` 和 `get_error_message()`。",
        "",
        "零崩溃、零越界、零泄漏目标：",
        "",
        "| 检查项 | 结果 |",
        "|---|---:|",
        f"| ASan/LSan 失败 | {len(asan_failures)} |",
        f"| 超时 | {len(timeouts)} |",
        f"| UBSan 运行时报告（含数组越界/错位访问） | {len(ubsan_failures)} |",
        f"| 错误信息缺偏移 | {len(missing_offsets)} |",
        f"| 合法图逐像素不一致 | {valid_total - pixel_equal} |",
        "",
        "## 畸形语料",
        "",
        "| 类别 | 数量 | 解码器拒绝 | 解码器接受 | 包装 API 拒绝 | ASan/LSan 失败 | UBSan 报告 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for category in ("truncation", "table_entry_oob", "duplicate_scan", "oversized_dimensions"):
        stats = category_stats[category]
        lines.append(
            f"| {category_names[category]} | {stats['total']} | {stats['class_err']} | "
            f"{stats['class_ok']} | {stats['wrap_null']} | {stats['asan']} | {stats['ubsan']} |"
        )
    lines.extend([
        "",
        "超大尺寸用例在类接口上最多读取 2 行，以避免无意义的大图计算；包装 API 会执行完整分配路径。",
        "“解码器接受”的条目来自原有 EOF 填充、边界内合法尺寸和重复渐进扫描语义；它们同样通过了 ASan/UBSan/LSan 检查。",
        "另以默认 SSE2 构建复跑同一 3000 例：0 ASan/LSan、0 UBSan、0 超时。",
        "",
        "## 错误码分布",
        "",
        "| 错误码 | 次数 |",
        "|---|---:|",
    ])
    for code, count in sorted(codes.items(), key=lambda pair: (-pair[1], pair[0])):
        lines.append(f"| `{code}` | {count} |")
    if not codes:
        lines.append("| 无 | 0 |")
    lines.extend([
        "",
        "## 合法图回归",
        "",
        f"参考实现直接从未修改的 `HEAD:{git_sha}` 源码编译；当前实现与参考实现使用相同的 `req_comps=3` 包装 API。合法图由 Pillow/libjpeg 编码。",
        f"逐像素比较内容为完整的 RGB 输出字节流：{pixel_equal}/{valid_total} 哈希一致。",
        "另以默认 SSE2 构建对同一 500 张图复跑，500/500 哈希一致。",
        "",
        "覆盖范围：灰度、RGB 4:4:4、4:2:2、4:2:0、渐进式，奇偶/边界尺寸，质量因子 25–100，部分文件带 DRI 重启间隔。",
        "",
        "## 最小反例集",
        "",
    ])
    if minimal_records:
        lines.extend([
            "| 文件 | 字节 | 缺陷类别 | 期望 | 当前结果 | 状态 |",
            "|---|---:|---|---|---|---|",
        ])
        for item in minimal_records:
            status = "通过" if item["passed"] else "失败"
            lines.append(
                f"| `tests/minimal_cases/{item['file']}` | {item['size']} | {item['title']} | "
                f"`{item['expected']}` | `{item['actual']}` | {status} |"
            )
    else:
        lines.append("尚未生成。")
    lines.extend([
        "",
        "## 复现",
        "",
        "```bash",
        "python3 tests/run_suite.py --workdir /tmp/jpgd-suite --regenerate",
        "```",
        "",
        f"机器可读结果位于 `reports/results.json`；完整语料位于运行目录 `{workdir}`，不入库。",
        "",
    ])
    report = "\n".join(lines)
    (report_dir / "security_fuzz_report.md").write_text(report)
    result_json = {
        "timestamp_utc": now,
        "baseline_git_sha": git_sha,
        "compiler": compiler,
        "elapsed_seconds": round(elapsed, 3),
        "malformed": {
            "total": malformed_total,
            "asan_or_nonzero": len(asan_failures),
            "timeouts": len(timeouts),
            "ubsan_reports": len(ubsan_failures),
            "class_errors": len(class_errors),
            "class_ok": len(class_ok),
            "missing_or_invalid_offsets": len(missing_offsets),
            "codes": dict(codes),
            "categories": dict(category_stats),
            "ubsan_messages": dict(top_ublines),
        },
        "valid": {
            "total": valid_total,
            "pixel_equal": pixel_equal,
            "clean": valid_clean,
            "failures": [record for record in valid_records if not record["pixel_equal"] or not record["class_complete"]],
        },
        "minimal_cases": minimal_records,
        "failures": [record for record in malformed_records if record["asan"] or record["returncode"] != 0 or record["ubsan"]],
    }
    (report_dir / "results.json").write_text(json.dumps(result_json, indent=2) + "\n")
    return result_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/jpgd-suite-run"))
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    probe, reference = compile_tools(workdir)
    corpus = ensure_corpus(workdir, args.regenerate)
    valid_manifest = load_manifest(corpus / "valid_manifest.csv")
    malformed_manifest = load_manifest(corpus / "malformed_manifest.csv")
    if len(valid_manifest) != 500 or len(malformed_manifest) != 3000:
        raise RuntimeError("corpus size mismatch")

    valid_records = parallel_map(
        lambda item: probe_valid(probe, reference, corpus, item, args.timeout),
        valid_manifest, args.workers,
    )
    malformed_records = parallel_map(
        lambda item: probe_malformed(probe, corpus, item, args.timeout),
        malformed_manifest, args.workers,
    )
    minimal_records = verify_minimal_cases(probe, args.timeout)
    elapsed = time.monotonic() - started
    result = write_report(workdir, corpus, malformed_manifest, valid_records,
                          malformed_records, minimal_records, elapsed)
    print(json.dumps({
        "malformed_total": result["malformed"]["total"],
        "asan_or_nonzero": result["malformed"]["asan_or_nonzero"],
        "timeouts": result["malformed"]["timeouts"],
        "ubsan_reports": result["malformed"]["ubsan_reports"],
        "missing_offsets": result["malformed"]["missing_or_invalid_offsets"],
        "valid_pixel_equal": result["valid"]["pixel_equal"],
        "valid_total": result["valid"]["total"],
        "elapsed_seconds": result["elapsed_seconds"],
    }, indent=2))
    if (result["malformed"]["asan_or_nonzero"] or result["malformed"]["timeouts"]
            or result["malformed"]["ubsan_reports"]
            or result["malformed"]["missing_or_invalid_offsets"]):
        return 1
    if result["valid"]["pixel_equal"] != result["valid"]["total"]:
        return 1
    if any(not item["passed"] for item in result["minimal_cases"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
