#!/usr/bin/env python3
"""断链缓存与断点补传验证（FR-M1-06）。

流程：健康探测 → 启动模拟器断链模式 → 断链期间数据落盘 → 恢复补传 →
轮询历史接口逐条核对「补齐条数」与「时间戳偏差」。

判据（FR-M1-06）：丢失条数 = 0，补传时间戳与原始采集时刻偏差 = 0。
正式口径是单机房 30 min / 约 43 万条；演练可用 --drop-seconds 缩短，
但缩短后的结果**不能**作为 FR-M1-06 的验收结论，报告里必须写明实际时长。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from _common import (
    add_common_args,
    bearer_headers,
    die,
    get_json,
    http_request,
    info,
    obtain_token,
    iso_from_ms,
    parse_iso_ms,
    print_table,
    require_base_url,
    warn,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = REPO_ROOT / "tools" / "simulator" / "main.py"


def load_manifest(path: Path, point_id: int) -> list[dict[str, Any]]:
    if not path.is_file():
        die(f"找不到清单文件 {path}",
            "先跑模拟器断链模式并带 --manifest，或去掉 --skip-run 让本脚本自行发起")
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if int(record.get("point_id", 0)) == point_id:
                entries.append(record)
    return entries


def fetch_series(base_url: str, api_prefix: str, point_id: int,
                 start_ms: int, end_ms: int, timeout: float,
                 token: str = "") -> tuple[list[dict[str, Any]], str]:
    # 对齐 P4 已实现的 /history（/series 属 P5 接口，现返回 501）
    path = f"{api_prefix}/points/{point_id}/history"
    result = http_request(base_url, path, timeout, params={
        "from": iso_from_ms(start_ms - 60_000),
        "to": iso_from_ms(end_ms + 60_000),
        "interval": "raw",
    }, headers=bearer_headers(token))
    if result.error:
        return [], result.error
    if not result.ok:
        return [], f"HTTP {result.status}"
    body = result.body
    if not isinstance(body, dict):
        return [], "响应不是 JSON 对象"
    items = body.get("points")
    route = "history/raw"
    return (items if isinstance(items, list) else []), route


def wait_for_backfill(base_url: str, api_prefix: str, point_id: int,
                      expected_ms: list[int], wait_seconds: float, timeout: float,
                      token: str = "",
                      ) -> tuple[set[int], str, int]:
    deadline = time.monotonic() + wait_seconds
    last_route = "unknown"
    attempts = 0
    while True:
        attempts += 1
        items, route = fetch_series(base_url, api_prefix, point_id,
                                    min(expected_ms), max(expected_ms), timeout, token)
        last_route = route
        seen = {value for value in (parse_iso_ms(str(item.get("ts", ""))) for item in items)
                if value is not None}
        present = sum(1 for ts in expected_ms if ts in seen)
        print(f"[poll] 第 {attempts} 次：应补 {len(expected_ms)}，已见 {present}，route={route}",
              flush=True)
        if present == len(expected_ms):
            return seen, last_route, attempts
        if time.monotonic() >= deadline:
            return seen, last_route, attempts
        time.sleep(5.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CDCIM 断链缓存与断点补传验证")
    add_common_args(parser)
    parser.add_argument("--point-id", type=int, default=1001, help="对账测点，默认 1001（快变点）")
    parser.add_argument("--device-id", type=int, default=1)
    parser.add_argument("--points", type=int, default=20, help="模拟器生成测点数，默认 20")
    parser.add_argument("--warmup", type=float, default=10.0, help="断链前正常运行秒数，默认 10")
    parser.add_argument("--drop-seconds", type=float, default=1800.0,
                        help="断链时长，默认 1800（FR-M1-06 官方口径）")
    parser.add_argument("--replay-rate", type=float, default=480.0, help="补传速率 msg/s")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="模拟器总时长；0 表示 warmup + drop-seconds + 60")
    parser.add_argument("--backfill-wait", type=float, default=300.0,
                        help="恢复后等待补齐的秒数，默认 300；官方口径 900")
    parser.add_argument("--manifest", default=str(REPO_ROOT / ".simulator" / "backfill-manifest.jsonl"))
    parser.add_argument("--stats", default=str(REPO_ROOT / ".simulator" / "backfill-stats.json"))
    parser.add_argument("--skip-run", action="store_true", help="不发起模拟器，只核对已有 manifest")
    parser.add_argument("--api-prefix", default="/api/v1")
    parser.add_argument("--json-out", default="", help="核对结果写入 JSON")
    args = parser.parse_args(argv)

    base_url = require_base_url(args.base_url)
    manifest_path = Path(args.manifest)
    stats_path = Path(args.stats)
    simulation_seconds = args.duration or (args.warmup + args.drop_seconds + 60.0)

    token = obtain_token(base_url, args.timeout)
    health = get_json(base_url, "/healthz", args.timeout)
    if health.error or not health.ok:
        die("服务不可达或 /healthz 不健康，补传验证无法进行。",
            f"{health.error or f'HTTP {health.status}'}；先跑 scripts/check_stack.py，"
            "并确认 api-service 与采集侧、总线、时序库均可达")

    if not args.skip_run:
        if not SIMULATOR.is_file():
            die(f"找不到模拟器 {SIMULATOR}")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, str(SIMULATOR),
            "--devices", str(max(1, args.device_id)),
            "--points", str(max(args.device_id, args.points)),
            "--fault", "drop",
            "--drop-after", str(args.warmup),
            "--drop-seconds", str(args.drop_seconds),
            "--replay-rate", str(args.replay_rate),
            "--duration", str(int(simulation_seconds)),
            "--manifest", str(manifest_path),
            "--stats-out", str(stats_path),
        ]
        info("执行：" + " ".join(command))
        if args.drop_seconds > 300:
            warn(f"断链 {args.drop_seconds:.0f}s，本次运行较长。",
                 "演练可先用 --drop-seconds 120 --backfill-wait 120；正式结论必须用 1800")
        completed = subprocess.run(command, timeout=simulation_seconds + 300)
        if completed.returncode != 0:
            die(f"模拟器退出码 {completed.returncode}", "查看上方的模拟器输出定位失败原因")

    entries = load_manifest(manifest_path, args.point_id)
    expected_ms = sorted({value for value in (parse_iso_ms(str(item.get("ts", ""))) for item in entries)
                          if value is not None})
    if not expected_ms:
        die(f"清单 {manifest_path} 中没有 point_id={args.point_id} 的记录。",
            "确认 --point-id 与模拟器实际生成的测点一致；快变点从 1001 起")

    info(f"清单口径：断链期间落盘 {len(entries)} 条，去重后 {len(expected_ms)} 个时间戳")
    seen, route, attempts = wait_for_backfill(
        base_url, args.api_prefix, args.point_id, expected_ms, args.backfill_wait, args.timeout, token,
    )

    missing = [ts for ts in expected_ms if ts not in seen]
    offsets = [min(abs(server_ts - ts) for server_ts in seen) if seen else -1 for ts in expected_ms]
    nonzero = sum(1 for value in offsets if value != 0)
    max_offset = max(offsets) if offsets else -1

    stats_body: dict[str, Any] = {}
    if stats_path.is_file():
        stats_body = json.loads(stats_path.read_text(encoding="utf-8"))

    rows = [
        ["断链时长(s)", f"{args.drop_seconds:.0f}"],
        ["应补条数", str(len(expected_ms))],
        ["已补齐", str(len(expected_ms) - len(missing))],
        ["缺失条数", str(len(missing))],
        ["时间戳偏差(最大,ms)", str(max_offset)],
        ["时间戳偏差非零条数", str(nonzero)],
        ["查询路由", route],
        ["轮询次数", str(attempts)],
        ["模拟器 online/replay/dropped",
         f"{stats_body.get('online_sent', 'N/A')}/{stats_body.get('replay_sent', 'N/A')}/"
         f"{stats_body.get('dropped', 'N/A')}"],
    ]
    print_table(["项", "结果"], rows)

    if route != "raw":
        warn(f"查询路由为 {route}，接口做了降采样，逐条时间戳对账可能失真。",
             "缩短窗口或提高 max_points；若 180 天区间被迫降采样，属接口 Q-2 的既定行为")

    passed = not missing and max_offset == 0 and stats_body.get("dropped", 0) == 0
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({
                "base_url": base_url,
                "point_id": args.point_id,
                "drop_seconds": args.drop_seconds,
                "expected": len(expected_ms),
                "missing": len(missing),
                "max_offset_ms": max_offset,
                "nonzero_offset": nonzero,
                "route": route,
                "simulator": stats_body,
                "pass": passed,
            }, handle, ensure_ascii=False, indent=2)
        info(f"核对结果已写入 {args.json_out}")

    print()
    if passed:
        info("PASS：补齐条数一致、时间戳偏差 0、队列无丢弃。")
        return 0
    print("[FAIL] 未满足 FR-M1-06 判据。", file=sys.stderr)
    if missing:
        print(f"  - 缺 {len(missing)} 条；首条 {iso_from_ms(missing[0])}", file=sys.stderr)
    if max_offset != 0:
        print(f"  - 时间戳偏差最大 {max_offset} ms，非零 {nonzero} 条", file=sys.stderr)
    if stats_body.get("dropped", 0):
        print(f"  - 模拟器队列丢弃 {stats_body.get('dropped')} 条，检查 --queue-quota-mb", file=sys.stderr)
    print("[HINT] 排查顺序：① 边端队列是否落盘（.simulator/edge-queue.jsonl 行数）"
          " ② 补传是否被限速消费组消费（telemetry.replay lag）"
          " ③ 幂等键是否把补传覆盖掉（同 ts 应覆盖而非丢弃）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
