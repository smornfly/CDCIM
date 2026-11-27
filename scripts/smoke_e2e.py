#!/usr/bin/env python3
"""端到端冒烟：发一条模拟数据 → 等待 → 查询最新值 → 断言一致，并打印各段耗时。

判据只对「这一条」成立。单条样本**不能**用于判定 NFR-PERF-01 的 P99 ≤ 2 s，
分位指标必须由 P6 的稳态压测（容量文档 S1）采集。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from _common import (
    add_common_args,
    die,
    get_json,
    info,
    parse_iso_ms,
    print_table,
    require_base_url,
    warn,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = REPO_ROOT / "tools" / "simulator" / "main.py"


def extract_latest(body: Any) -> dict[str, Any] | None:
    """兼容 /realtime/points（items 数组）与 /points/{id}/latest（单对象）。"""
    if isinstance(body, dict):
        items = body.get("items")
        if isinstance(items, list) and items:
            first = items[0]
            return first if isinstance(first, dict) else None
        if "value" in body or "ts" in body:
            return body
    return None


def query_latest(base_url: str, api_prefix: str, point_id: int, timeout: float) -> tuple[dict[str, Any] | None, str, float, int]:
    """按接口文档优先 /realtime/points，404 时回落单点接口。返回 (数据, 路径, 耗时ms, 状态码)。"""
    candidates = [
        (f"{api_prefix}/realtime/points", {"point_ids": str(point_id)}),
        (f"{api_prefix}/points/{point_id}/latest", None),
    ]
    last_status = 0
    for path, params in candidates:
        result = get_json(base_url, path, timeout, params=params)
        last_status = result.status
        if result.ok:
            return extract_latest(result.body), path, result.elapsed_ms, result.status
        if result.status not in (404, 405):
            return None, f"{path} -> HTTP {result.status}", result.elapsed_ms, result.status
    return None, "两个候选路径均未返回数据", 0.0, last_status


def values_equal(actual: Any, expected: float) -> bool:
    if isinstance(actual, bool):
        return bool(actual) == bool(expected)
    try:
        return abs(float(actual) - float(expected)) <= 1e-6
    except (TypeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CDCIM 采集→入库→可查 端到端冒烟")
    add_common_args(parser)
    parser.add_argument("--point-id", type=int, default=1001, help="测点 ID，默认 1001")
    parser.add_argument("--device-id", type=int, default=1, help="设备 ID（仅用于模拟器选点），默认 1")
    parser.add_argument("--value", type=float, default=42.5, help="注入的固定值，默认 42.5")
    parser.add_argument("--wait", type=float, default=5.0, help="发布后先等待的秒数，默认 5")
    parser.add_argument("--deadline", type=float, default=20.0,
                        help="等待可查询的总时长上限（秒），默认 20")
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--api-prefix", default="/api/v1", help="接口前缀，默认 /api/v1")
    parser.add_argument("--simulator", default=str(SIMULATOR), help="模拟器脚本路径")
    parser.add_argument("--skip-publish", action="store_true", help="不发布，只查询当前最新值")
    args = parser.parse_args(argv)

    base_url = require_base_url(args.base_url)
    if not args.skip_publish and not Path(args.simulator).is_file():
        die(f"找不到模拟器 {args.simulator}", "确认仓库完整，或用 --simulator 指定路径")

    health = get_json(base_url, "/healthz", args.timeout)
    if health.error:
        die("服务不可达，冒烟无法开始。",
            f"{health.error}；先跑 scripts/check_stack.py --base-url {base_url}")
    if not health.ok:
        warn(f"/healthz 返回 HTTP {health.status}，仍继续（旁路降级不应阻断主链路）")

    publish_ms = 0.0
    if not args.skip_publish:
        points_needed = max(1, min(args.point_id - 1000, 2000))
        if args.point_id < 1001:
            die("point-id 必须 ≥ 1001（模拟器测点 ID 从 1001 起）",
                "换一个已存在的 point_id，或先用 POST /points/import 导入台账")
        command = [
            sys.executable, str(args.simulator),
            "--devices", str(max(1, args.device_id)),
            "--points", str(points_needed),
            "--only-point-id", str(args.point_id),
            "--fixed-value", str(args.value),
            "--duration", "3",
            "--drain-seconds", "0.5",
        ]
        info(f"发布 1 条：point_id={args.point_id} value={args.value}（{Path(args.simulator).name}）")
        started = time.perf_counter()
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
        publish_ms = (time.perf_counter() - started) * 1000
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()
            detail = tail[-1] if tail else f"退出码 {completed.returncode}"
            die("模拟器发布失败。", f"{detail}；检查 broker 可达性与 paho-mqtt 是否安装")

    t0_ms = int(time.time() * 1000)
    info(f"等待 {args.wait:.1f}s 后查询最新值（上限 {args.deadline:.0f}s）")
    time.sleep(args.wait)

    deadline = time.perf_counter() + max(args.deadline - args.wait, 0.0)
    record: dict[str, Any] | None = None
    hit_path = ""
    query_ms = 0.0
    found_ms = 0.0
    while True:
        record, path, elapsed_ms, status = query_latest(base_url, args.api_prefix, args.point_id, args.timeout)
        if record is not None and values_equal(record.get("value"), args.value):
            hit_path = path
            query_ms = elapsed_ms
            found_ms = time.time() * 1000 - t0_ms
            break
        if time.perf_counter() >= deadline:
            hit_path = path
            query_ms = elapsed_ms
            break
        time.sleep(args.poll_interval)

    if record is None:
        print_table(
            ["项", "值"],
            [
                ["查询路径", hit_path],
                ["结果", "未取到最新值"],
                ["建议", "确认 point_id 已入台账、模拟器 topic 前缀与采集网关订阅一致"],
            ],
        )
        print("\n[FAIL] 端到端断言失败：接口未返回该测点的最新值。", file=sys.stderr)
        return 1

    actual = record.get("value")
    ts_text = str(record.get("ts", ""))
    ts_ms = parse_iso_ms(ts_text)
    stale = record.get("stale")
    consistent = values_equal(actual, args.value)

    rows = [
        ["注入值", f"{args.value}"],
        ["查询值", f"{actual}"],
        ["值一致", "是" if consistent else "否"],
        ["质量码", str(record.get("quality", "N/A"))],
        ["数据时间戳", ts_text or "N/A"],
        ["stale 标记", str(stale) if stale is not None else "N/A"],
        ["命中路径", hit_path],
        ["发布阶段耗时(ms)", f"{publish_ms:.1f}"],
        ["查询接口耗时(ms)", f"{query_ms:.1f}"],
        ["发布→可查(ms)", f"{found_ms:.1f}" if found_ms else "N/A"],
    ]
    print_table(["项", "观测值"], rows)

    if ts_ms is not None and ts_ms < t0_ms - 60_000:
        warn("返回的时间戳早于本次发布 60 s 以上，可能是历史缓存值。",
             "提高 --value 的唯一性或先确认 last_row 缓存已刷新")

    print()
    if consistent:
        info("PASS：注入值与查询值一致，采集→入库→可查链路连通。")
        warn("单条样本不代表分位指标。",
             "P99 ≤ 2 s 只在 P6 稳态压测（2000 测点 × 10 min）中判定，见容量文档 §7 S1")
        return 0
    print(f"[FAIL] 值不一致：期望 {args.value}，实际 {actual}", file=sys.stderr)
    print("[HINT] 若同一 point_id 有多个设备在发数，查询会落到最后一条；"
          "改用 --skip-publish 之外的唯一 point_id 重试", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
