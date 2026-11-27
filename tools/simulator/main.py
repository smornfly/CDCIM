#!/usr/bin/env python3
"""CDCIM 设备模拟器（P4）。"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import random
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.2.0"
PERIOD_FAST_S = 5.0
PERIOD_SLOW_S = 30.0
DEVICE_TYPES = ("UPS", "AC", "PDU", "ENV", "BAT", "DOOR")

STOP = threading.Event()


# ---------------------------------------------------------------- 点位模型
@dataclass
class Point:
    device_id: int
    device_code: str
    point_id: int
    point_code: str
    period_s: float
    value_type: str
    nominal: float
    amplitude: float
    phase: float
    next_due: float = 0.0


@dataclass
class Stats:
    online: int = 0
    replay: int = 0
    buffered: int = 0
    dropped: int = 0
    queue_bytes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_online(self, n: int = 1) -> None:
        with self.lock:
            self.online += n

    def add_replay(self, n: int = 1) -> None:
        with self.lock:
            self.replay += n

    def add_buffered(self, size: int) -> None:
        with self.lock:
            self.buffered += 1
            self.queue_bytes += size

    def add_dropped(self, n: int = 1) -> None:
        with self.lock:
            self.dropped += n

    def remove_buffered(self, size: int) -> None:
        with self.lock:
            self.queue_bytes = max(0, self.queue_bytes - size)

    def snapshot(self) -> tuple[int, int, int, int, int]:
        with self.lock:
            return (self.online, self.replay, self.buffered,
                    self.dropped, self.queue_bytes)


# ---------------------------------------------------------------- 工具函数
def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def build_points(devices: int, total_points: int, fast_ratio: float) -> list[Point]:
    """按设备轮转分配测点，快变/慢变按比例前缀划分。"""
    devices = max(1, min(devices, total_points))
    fast_total = int(round(total_points * fast_ratio))
    points: list[Point] = []
    point_id = 1001
    for index in range(total_points):
        device_index = index % devices
        device_id = device_index + 1
        device_code = f"{DEVICE_TYPES[device_index % len(DEVICE_TYPES)]}-{device_id:04d}"
        is_fast = index < fast_total
        is_bool = (index % 10 == 9)
        points.append(
            Point(
                device_id=device_id,
                device_code=device_code,
                point_id=point_id + index,
                point_code=f"{device_code}-P{index % 20 + 1:02d}",
                period_s=PERIOD_FAST_S if is_fast else PERIOD_SLOW_S,
                value_type="bool" if is_bool else "double",
                nominal=220.0 if index % 3 == 0 else 26.0,
                amplitude=4.0,
                phase=random.random() * math.tau,
            )
        )
    return points


def sample_value(point: Point, elapsed: float, fault: str, duration: float) -> float | bool:
    """按故障形态产生工程值。返回 bool 的测点走 0/1 翻转。"""
    if point.value_type == "bool":
        return bool(int(elapsed // 60 + point.phase) % 2)
    if fault == "step" and duration > 0 and elapsed > duration / 2:
        return round(point.nominal * 1.2, 3)
    if fault == "drift":
        slope = point.amplitude / max(duration, 1.0) * 2.0
        return round(point.nominal + slope * elapsed, 3)
    wave = point.amplitude * math.sin(math.tau * elapsed / 300.0 + point.phase)
    noise = random.gauss(0.0, point.amplitude / 20.0)
    return round(point.nominal + wave + noise, 3)


def build_payload(point: Point, value: float | bool, replay: bool) -> bytes:
    now = iso_now()
    payload = {
        "event_id": str(uuid.uuid4()),
        "event_type": "telemetry.sample",
        "schema_version": SCHEMA_VERSION,
        "occurred_at": now,
        "produced_at": now,
        "producer": "tools/simulator",
        "trace_id": uuid.uuid4().hex[:16],
        "partition_key": point.device_code,
        "device_id": point.device_id,
        "device_code": point.device_code,
        "point_id": point.point_id,
        "point_code": point.point_code,
        "ts": now,
        "value": value,
        "value_type": point.value_type,
        "quality": "good",
        "ts_source": "replay" if replay else "device",
        "raw_value": value if isinstance(value, (int, float)) else None,
        "replay": replay,
        "is_late": False,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------- 发布通道
class Publisher:
    """dry-run 与 MQTT 两种实现，统一 publish 接口。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.dry_run = args.dry_run
        self.topic_prefix = args.topic_prefix.rstrip("/")
        self.client = None
        if self.dry_run:
            return
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import-not-found]
        except ImportError:
            raise SystemExit(
                "缺少 paho-mqtt。请执行 `pip install -r tools/simulator/requirements.txt`，"
                "或加 --dry-run 只做本机统计。"
            )
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"cdcim-sim-{uuid.uuid4().hex[:6]}")
        client.reconnect_delay_set(min_delay=1, max_delay=5)
        client.connect(args.broker, args.port, keepalive=30)
        client.loop_start()
        self.client = client
        self.qos = args.qos

    def publish(self, point: Point, payload: bytes, replay: bool) -> bool:
        if self.dry_run:
            return True
        topic = f"{self.topic_prefix}/{point.device_code}/telemetry"
        info = self.client.publish(topic, payload, qos=self.qos)  # type: ignore[union-attr]
        return info.rc == 0

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()


# ---------------------------------------------------------------- 主流程
def run(args: argparse.Namespace) -> int:
    if args.seed is not None:
        random.seed(args.seed)
    points = build_points(args.devices, args.points, args.fast_ratio)
    if args.only_point_id:
        points = [p for p in points if p.point_id == args.only_point_id]
        if not points:
            raise SystemExit(f"--only-point-id {args.only_point_id} 不在生成的测点区间内")

    base_rate = sum(1.0 / p.period_s for p in points)
    speed = 1.0
    if args.rate and args.rate > 0:
        speed = args.rate / base_rate
        print(f"[warn] --rate {args.rate} 会把发送间隔压缩到 1/{speed:.2f}，"
              f"仅用于压测，不代表真实采集周期", flush=True)

    publisher = Publisher(args)
    stats = Stats()
    queue_path = Path(args.queue_file) if args.fault == "drop" else None
    manifest_path = Path(args.manifest) if args.manifest else None
    queue_handle = None
    manifest_handle = None
    if queue_path is not None:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_handle = queue_path.open("a", encoding="utf-8")
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_handle = manifest_path.open("w", encoding="utf-8")

    started = time.monotonic()
    wall_started = datetime.now(timezone.utc).astimezone()
    deadline = started + args.duration if args.duration > 0 else float("inf")
    drop_begin = started + args.drop_after if args.fault == "drop" else float("inf")
    drop_end = drop_begin + args.drop_seconds if args.fault == "drop" else float("inf")
    next_stats = started + 10.0
    heap = [(started + random.random() * 0.5, index) for index, _ in enumerate(points)]
    heapq.heapify(heap)
    last_stats_at = started

    def on_signal(signum: int, _frame: object) -> None:
        print(f"\n[info] 收到信号 {signum}，准备退出", flush=True)
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    dropped_window_done = False
    replay_thread: threading.Thread | None = None
    try:
        while not STOP.is_set():
            now = time.monotonic()
            if now >= deadline:
                break
            if not heap:
                break
            due_at, index = heap[0]
            if due_at > now:
                time.sleep(min(due_at - now, 0.05))
                continue
            heapq.heappop(heap)
            point = points[index]
            elapsed = now - started
            in_drop = drop_begin <= now < drop_end
            value = (
                float(args.fixed_value)
                if args.fixed_value is not None
                else sample_value(point, elapsed, args.fault, args.duration)
            )
            payload = build_payload(point, value, replay=False)

            if in_drop:
                if stats.snapshot()[4] + len(payload) <= args.queue_quota_mb * 1024 * 1024:
                    queue_handle.write(payload.decode("utf-8") + "\n")  # type: ignore[union-attr]
                    queue_handle.flush()  # type: ignore[union-attr]
                    stats.add_buffered(len(payload))
                    if manifest_handle is not None:
                        record = json.loads(payload)
                        manifest_handle.write(json.dumps(
                            {"ts": record["ts"], "point_id": record["point_id"],
                             "device_code": record["device_code"], "value": record["value"]},
                            ensure_ascii=False) + "\n")
                else:
                    stats.add_dropped()
            else:
                if publisher.publish(point, payload, replay=False):
                    stats.add_online()
                else:
                    stats.add_dropped()

            interval = point.period_s / speed
            jitter = interval * args.jitter * random.uniform(-1.0, 1.0)
            heapq.heappush(heap, (due_at + max(0.001, interval + jitter), index))

            if now >= next_stats:
                online, replay, buffered, dropped, queue_bytes = stats.snapshot()
                window = max(now - last_stats_at, 0.001)
                actual_rate = (online + replay) / window
                print(
                    f"[stats] t={now - started:7.1f}s sent={online + replay} "
                    f"rate={actual_rate:8.2f}/s online={online} replay={replay} "
                    f"buffered={buffered} dropped={dropped} "
                    f"queue={queue_bytes / 1024 / 1024:.1f}MB",
                    flush=True,
                )
                last_stats_at = now
                next_stats += 10.0

            if not dropped_window_done and now >= drop_end and queue_handle is not None:
                dropped_window_done = True
                queue_handle.close()
                queue_handle = None
                print(f"[info] 断链窗口结束，开始补传 {queue_path}；"
                      f"速率上限 {args.replay_rate:.0f}/s", flush=True)
                replay_thread = threading.Thread(
                    target=replay_queue,
                    args=(publisher, points, queue_path, args.replay_rate, stats),
                    daemon=True,
                )
                replay_thread.start()
        if replay_thread is not None and replay_thread.is_alive():
            print(f"[info] 等待补传线程收尾（上限 {args.replay_grace:.0f}s）", flush=True)
            replay_thread.join(timeout=args.replay_grace)
            if replay_thread.is_alive():
                print("[warn] 补传未在宽限期内完成，后续条数需人工核对", flush=True)

    finally:
        if queue_handle is not None:
            queue_handle.close()
        if manifest_handle is not None:
            manifest_handle.close()
        publisher.close()

    time.sleep(args.drain_seconds)
    loop_elapsed = time.monotonic() - started - args.drain_seconds
    online, replay, buffered, dropped, queue_bytes = stats.snapshot()
    summary = {
        "started_at": wall_started.isoformat(timespec="seconds"),
        "ended_at": iso_now(),
        "online_sent": online,
        "replay_sent": replay,
        "buffered": buffered,
        "dropped": dropped,
        "queue_bytes": queue_bytes,
        "fault": args.fault,
        "duration_s": round(max(loop_elapsed, 0.0), 1),
    }
    if args.stats_out:
        Path(args.stats_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[summary] {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return 0


def replay_queue(publisher: Publisher, points: list[Point], queue_path: Path,
                 replay_rate: float, stats: Stats) -> None:
    """按固定速率回放落盘队列，保留原始 ts 与 ts_source=replay。"""
    by_key = {(p.device_code, p.point_id): p for p in points}
    interval = 1.0 / max(replay_rate, 1.0)
    with queue_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if STOP.is_set():
                return
            record = json.loads(line)
            point = by_key.get((record["device_code"], record["point_id"]))
            if point is None:
                continue
            payload = json.dumps({**record, "ts_source": "replay", "replay": True},
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if publisher.publish(point, payload, replay=True):
                stats.add_replay()
                stats.remove_buffered(len(line))
            else:
                stats.add_dropped()
            time.sleep(interval)
    print(f"[info] 补传完成，累计 {stats.snapshot()[1]} 条", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CDCIM 设备模拟器：按 5 s / 30 s 分频生成 telemetry.sample 报文并发布到 MQTT",
    )
    parser.add_argument("--devices", type=int, default=300, help="设备数，默认 300")
    parser.add_argument("--points", type=int, default=2000, help="测点总数，默认 2000")
    parser.add_argument("--fast-ratio", type=float, default=0.2,
                        help="快变测点占比，默认 0.2（快变 5 s，慢变 30 s）")
    parser.add_argument("--rate", type=float, default=0.0,
                        help="目标聚合速率 msg/s；0 表示按 5 s/30 s 设计节奏。压测用，会等比压缩间隔")
    parser.add_argument("--duration", type=int, default=120, help="运行时长（秒），默认 120；0 表示不限期")
    parser.add_argument("--fault", choices=("normal", "step", "drift", "drop"), default="normal")
    parser.add_argument("--drop-after", type=float, default=30.0, help="断链开始时刻（秒），默认 30")
    parser.add_argument("--drop-seconds", type=float, default=1800.0,
                        help="断链时长（秒），默认 1800（对应 FR-M1-06 的单机房断网 30 min）")
    parser.add_argument("--replay-grace", type=float, default=600.0,
                        help="主循环结束后等待补传线程收尾的秒数，默认 600")
    parser.add_argument("--replay-rate", type=float, default=480.0,
                        help="补传速率 msg/s，默认 480（43 万条 15 min 补齐，见 FR-M1-06）")
    parser.add_argument("--queue-file", default=".simulator/edge-queue.jsonl",
                        help="断链落盘队列路径")
    parser.add_argument("--queue-quota-mb", type=float, default=150.0,
                        help="落盘队列配额 MB，默认 150（FR-M1-06）")
    parser.add_argument("--manifest", default="", help="断链期间落盘记录的清单（供 verify_backfill 对账）")
    parser.add_argument("--stats-out", default="", help="运行汇总 JSON 输出路径")
    parser.add_argument("--only-point-id", type=int, default=0, help="只发送指定 point_id")
    parser.add_argument("--fixed-value", type=float, default=None, help="所有测点固定为该值")
    parser.add_argument("--broker", default=os.getenv("CDCIM_MQTT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CDCIM_MQTT_PORT", "1883")))
    parser.add_argument("--topic-prefix", default=os.getenv("CDCIM_MQTT_TOPIC_PREFIX", "cdcim/site-a"),
                        help="前缀 + /<device_code>/telemetry，须匹配采集订阅 cdcim/+/+/telemetry")
    parser.add_argument("--qos", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--jitter", type=float, default=0.05, help="发送间隔抖动比例，默认 5%%")
    parser.add_argument("--drain-seconds", type=float, default=2.0, help="退出前等待在途消息的时间")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="不连 broker，只做本机节奏与统计自检")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
