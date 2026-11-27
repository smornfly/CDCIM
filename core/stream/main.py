"""流计算：``telemetry.raw`` → TDengine 明细批写 + 60 s 降采样。

链路::

    cg-compute 消费 telemetry.raw
      ├─ 攒批 500 条 或 200 ms（先到者触发）
      ├─ INSERT INTO dcim.m_<point_id> USING dcim.st_metric TAGS (...)
      └─ 喂 60 s 滚动窗口 → 窗口关闭即写 dcim_1m.m_<point_id>

四条与设计文档挂钩的实现决定：

1. **子表懒建**（DB §4.2）：``CREATE TABLE IF NOT EXISTS`` 幂等，一测点一子表
   （``m_<point_id>``）。写入 ``ts`` 用**毫秒整数**而非时间字符串——库端对无偏移
   时间字符串的解释依赖 server 时区，一旦容器 TZ 改动，历史数据就会整体平移，
   而"时间戳不被加工"是 D-31 的底线。
2. **单分区 → worker = 1**（进程视图 §3.2）：``telemetry.raw`` 只有 1 个分区，
   同组并行度上限即 1；起第二个进程只会被 rebalance 踢出并引发 repeated rebalance。
3. **写失败重试 3 次（1/2/4 s + 抖动）后落 DLQ 并提交位点**（契约 §7.2）：
   单分区上"卡住"等于 4000 个测点全停，故把"不阻塞"排在"不丢"之前。
4. **聚合只调 :func:`window.aggregate`**：在线与重算共用同一函数（ADR-0007）。

P4 骨架的已知简化（详见《CDCIM-P4-DD-01》§5）：TAG 值 ``room_id`` / ``device_type``
用占位值，P5 由 ``meta.change`` 消费快照填充。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

import argparse
import asyncio
import json
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from taosrest import connect as taos_connect

from cdcim_shared.config import Settings, get_settings
from cdcim_shared.contracts import TelemetrySample
from cdcim_shared.health import serve_with_health
from cdcim_shared.logging import configure_logging, get_logger, new_trace_id, trace_scope
from cdcim_shared.timeutil import to_iso_ms, to_millis, utc_now
from cdcim_shared.topics import ConsumerGroup, EventType, Topic
from window import TumblingWindowAggregator, WindowResult

SERVICE = "stream-compute"

#: 契约 §7.2：3 次重试、1/2/4 s、+20% 抖动，总时长 ≤ 8 s
RETRY_DELAYS_S = (1.0, 2.0, 4.0)
RETRY_JITTER = 0.2

#: E14 / E15 的存储编码（与 DB §4.1 ``quality TINYINT`` / ``ts_source TINYINT`` 一致）
QUALITY_CODES = {"good": 0, "uncertain": 1, "bad": 2, "stale": 3}
TS_SOURCE_CODES = {"device": 0, "gateway": 1, "replay": 2, "manual": 3}

log = get_logger(__name__)


@dataclass(frozen=True)
class DeviceTags:
    """``st_metric`` 的 5 个 TAG。P4 只有 ``device_id`` 是真实值。"""

    device_id: int
    room_id: int = 0
    rack_id: int = 0
    device_type: str = "unknown"
    critical_flag: int = 0

    def as_sql(self) -> str:
        """TAG 字面量。``device_type`` 由固定字母表校验，SQL 内不拼接任何外部原文。"""
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_"
        safe_type = "".join(ch for ch in self.device_type if ch in alphabet)[:24]
        return (
            f"{int(self.device_id)}, {int(self.room_id)}, {int(self.rack_id)}, "
            f"'{safe_type or 'unknown'}', {1 if self.critical_flag else 0}"
        )


class MetaSnapshot:
    """设备 TAG 的来源。P5 替换点：由 ``cg-meta`` 消费 ``meta.change`` 维护内存快照。

    P4 用占位值（``room_id=0``、``device_type='unknown'``）保证 DDL 合法且可写入；
    这些占位值**不参与任何查询过滤**，因此不会污染结果，只是让"设备归属"这一列暂时缺省。
    """

    def tags_for(self, device_id: int) -> DeviceTags:
        return DeviceTags(device_id=device_id)


class TDengineWriter:
    """TDengine REST 写入器（taospy 的 ``taosrest``，不依赖 native 客户端库）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: Any | None = None
        self._known_subtables: set[int] = set()

    def _connection(self) -> Any:
        if self._conn is None:
            self._conn = taos_connect(
                url=f"http://{self._settings.tdengine_host}:{self._settings.tdengine_rest_port}",
                user=self._settings.tdengine_user,
                password=self._settings.tdengine_password.get_secret_value(),
                timeout=self._settings.tdengine_timeout_s,
            )
        return self._conn

    def _execute(self, sql: str) -> None:
        if self._settings.tdengine_dry_run:
            log.info("tdengine_dry_run", msg="sql not executed", sql=sql[:400])
            return
        cursor = self._connection().cursor()
        try:
            cursor.execute(sql)
        finally:
            cursor.close()

    def ensure_subtable(self, tags: DeviceTags, point_id: int) -> None:
        """一测点一子表；已建过则跳过（进程内缓存，避免每条 INSERT 前都 CREATE）。"""
        if point_id in self._known_subtables:
            return
        db = self._settings.tdengine_database
        self._execute(
            f"CREATE TABLE IF NOT EXISTS {db}.m_{point_id} USING {db}.st_metric TAGS ({tags.as_sql()})"
        )
        self._known_subtables.add(point_id)

    def insert_samples(self, rows: list[TelemetrySample], tags_by_device: dict[int, DeviceTags]) -> int:
        """按测点分组，一个测点一条多行 INSERT（同一子表的多行合并，减少写放大）。"""
        by_point: dict[int, list[TelemetrySample]] = {}
        for sample in rows:
            by_point.setdefault(sample.point_id, []).append(sample)

        db = self._settings.tdengine_database
        written = 0
        for point_id, samples in by_point.items():
            tags = tags_by_device.get(samples[0].device_id) or DeviceTags(samples[0].device_id)
            self.ensure_subtable(tags, point_id)
            values = ",".join(_sample_values(s) for s in samples)
            self._execute(
                f"INSERT INTO {db}.m_{point_id} USING {db}.st_metric TAGS ({tags.as_sql()}) VALUES {values}"
            )
            written += len(samples)
        return written

    def insert_windows(self, results: list[WindowResult]) -> int:
        """写 1 min 降采样表。``ts`` 取窗口起点（左闭），与查询侧 ``bucket`` 对齐。"""
        if not results:
            return 0
        db = self._settings.tdengine_downsample_db
        written = 0
        for result in results:
            agg = result.agg
            tags = f"{result.device_id}, 0, 'unknown', 'unknown'"
            values = (
                f"({result.window_start_ms}, {result.point_id}, "
                f"{_num(agg.val_avg)}, {_num(agg.val_max)}, {_num(agg.val_min)}, "
                f"{_num(agg.val_first)}, {_num(agg.val_last)}, "
                f"{agg.sample_count}, {agg.valid_count}, {agg.quality_flag}, "
                f"{self._settings.agg_version})"
            )
            self._execute(
                f"INSERT INTO {db}.m_{result.point_id} USING {db}.st_metric_1m TAGS ({tags}) VALUES {values}"
            )
            written += 1
        return written


def _num(value: float | None) -> str:
    """浮点字面量。``nan``/``inf`` 写成 NULL——TDengine 不接受这两个字面量。"""
    if value is None or not math.isfinite(value):
        return "NULL"
    return repr(float(value))


def _sample_values(sample: TelemetrySample) -> str:
    """一行的字面量。所有外部字段都经过类型转换或枚举映射，SQL 里不出现原始字符串。"""
    val_double = "NULL"
    val_int = "NULL"
    val_bool = "NULL"
    if isinstance(sample.value, bool):
        val_bool = "true" if sample.value else "false"
    elif isinstance(sample.value, int):
        val_int = str(sample.value)
    elif isinstance(sample.value, float):
        val_double = _num(sample.value)

    raw_val = _num(sample.raw_value)
    # source_batch 只允许 [A-Za-z0-9_-]，非法字符剔除（防注入，同时保持可读）
    batch = "".join(ch for ch in (sample.source_batch or "") if ch.isalnum() or ch in "-_")
    batch_literal = f"'{batch}'" if batch else "NULL"

    return (
        f"({to_millis(sample.ts)}, {sample.point_id}, {val_double}, {val_int}, {val_bool}, "
        f"{QUALITY_CODES[sample.quality]}, {TS_SOURCE_CODES[sample.ts_source]}, {raw_val}, "
        f"{to_millis(sample.produced_at)}, {batch_literal}, {1 if sample.is_late else 0})"
    )


class StreamWorker:
    """消费 → 攒批 → 写库 → 提交位点。单协程，无锁。"""

    def __init__(self, settings: Settings, writer: TDengineWriter) -> None:
        self._settings = settings
        self._writer = writer
        self._windows = TumblingWindowAggregator(settings.window_size_s)
        self._meta = MetaSnapshot()
        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._stop = asyncio.Event()
        self.counters: dict[str, int] = {
            "consumed": 0,
            "written": 0,
            "windows_closed": 0,
            "window_rows": 0,
            "late": 0,
            "dlq": 0,
            "retry": 0,
        }

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            Topic.TELEMETRY_RAW,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=ConsumerGroup.COMPUTE,
            enable_auto_commit=False,          # 手动提交：先写库成功再提交（至少一次）
            auto_offset_reset="earliest",
            max_poll_records=self._settings.consumer_fetch_max_records,
        )
        await self._consumer.start()
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            acks=self._settings.kafka_acks,
            linger_ms=self._settings.kafka_linger_ms,
            compression_type=self._settings.kafka_compression,
        )
        await self._producer.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._consumer is not None:
            await self._consumer.stop()
        if self._producer is not None:
            await self._producer.stop()

    # ------------------------------------------------------------------ 主循环

    async def run(self, *, once: bool = False, once_timeout_s: float = 10.0) -> int:
        await self.start()
        assert self._consumer is not None
        try:
            if once:
                return await self._run_once(once_timeout_s)
            ticker = asyncio.create_task(self._window_tick_loop(), name="window-tick")
            try:
                while not self._stop.is_set():
                    await self._consume_cycle()
            finally:
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
            return 0
        finally:
            await self.stop()

    async def _run_once(self, timeout_s: float) -> int:
        assert self._consumer is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            batch = await self._collect_batch()
            if batch:
                await self._flush(batch)
                log.info("once_done", msg="single batch processed", **self.counters)
                return 0
        log.warning("once_timeout", msg="no record consumed within timeout")
        return 0

    async def _consume_cycle(self) -> None:
        batch = await self._collect_batch()
        if batch:
            await self._flush(batch)

    async def _collect_batch(self) -> list[TelemetrySample]:
        """攒批：500 条或 200 ms，先到者触发（契约 §6、数据架构 §3.5）。"""
        assert self._consumer is not None
        batch: list[TelemetrySample] = []
        deadline = time.monotonic() + self._settings.write_batch_max_delay_ms / 1000
        while len(batch) < self._settings.write_batch_max_rows:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            records = await self._consumer.getmany(
                timeout_ms=min(remaining_ms, self._settings.consumer_poll_timeout_ms),
                max_records=self._settings.consumer_fetch_max_records,
            )
            for messages in records.values():
                for message in messages:
                    sample = self._decode(message.value)
                    if sample is not None:
                        batch.append(sample)
            if batch and time.monotonic() >= deadline:
                break
        return batch[: self._settings.write_batch_max_rows]

    def _decode(self, payload: bytes) -> TelemetrySample | None:
        """反序列化失败属不可重试错误（契约 §7.1）：计 DLQ 并跳过，绝不阻塞分区。"""
        try:
            sample = TelemetrySample.model_validate_json(payload)
        except ValueError as exc:
            self.counters["dlq"] += 1
            log.error("sample_invalid", msg=str(exc)[:200], payload_bytes=len(payload))
            return None
        self.counters["consumed"] += 1
        return sample

    # ------------------------------------------------------------------ 写库

    async def _flush(self, batch: list[TelemetrySample]) -> None:
        tags_by_device = {s.device_id: self._meta.tags_for(s.device_id) for s in batch}
        written = await self._with_retry(
            "insert_samples", lambda: self._writer.insert_samples(batch, tags_by_device)
        )
        if written is None:
            await self._to_dlq(batch, error_class="downstream_unavailable")
            await self._commit()
            return

        self.counters["written"] += written
        windows = self._windows.ingest_many(batch)
        closed = await self._with_retry(
            "insert_windows", lambda: self._writer.insert_windows(windows)
        )
        if closed is None:
            # 明细已落库；聚合失败不回滚明细（两条写路径独立，见数据架构 §3.5）
            await self._to_dlq_windows(windows)
        else:
            self.counters["windows_closed"] += len(windows)
            self.counters["window_rows"] += closed
        await self._commit()

    async def _with_retry(self, operation: str, call: Callable[[], int]) -> int | None:
        """3 次退避重试，总时长 ≤ 8 s；仍失败返回 None（由调用方落 DLQ）。"""
        for attempt, delay in enumerate((0.0, *RETRY_DELAYS_S)):
            if delay:
                jittered = delay * (1 + random.uniform(-RETRY_JITTER, RETRY_JITTER))
                await asyncio.sleep(jittered)
            try:
                # 同步 DB 驱动一律离开事件循环（PY-06）
                return await asyncio.to_thread(call)
            except Exception as exc:  # noqa: BLE001 - 下游故障类型不可穷举
                self.counters["retry"] += 1
                log.warning(
                    "tdengine_write_failed",
                    msg="write attempt failed",
                    operation=operation,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                )
        return None

    async def _commit(self) -> None:
        assert self._consumer is not None
        try:
            await self._consumer.commit()
        except Exception as exc:  # noqa: BLE001 - 位点提交失败会在下次 poll 重试
            log.error("offset_commit_failed", msg=str(exc)[:200])

    # ------------------------------------------------------------------ 旁路

    async def _to_dlq(self, batch: list[TelemetrySample], error_class: str) -> None:
        if self._producer is None or not batch:
            return
        payload = {
            "event_type": EventType.DEAD_LETTER,
            "schema_version": "1.0.0",
            "dlq_id": new_trace_id(),
            "origin_topic": Topic.TELEMETRY_RAW,
            "origin_partition": 0,
            "origin_offset": -1,
            "consumer_group": ConsumerGroup.COMPUTE,
            "error_class": error_class,
            "error_message": f"batch of {len(batch)} samples failed after retries",
            "attempt_count": len(RETRY_DELAYS_S) + 1,
            "first_failed_at": to_iso_ms(utc_now()),
            "payload_ref": None,
        }
        try:
            await self._producer.send_and_wait(
                Topic.DLQ_DEFAULT, value=json.dumps(payload, ensure_ascii=False).encode()
            )
            self.counters["dlq"] += len(batch)
        except Exception as exc:  # noqa: BLE001 - DLQ 也失败时只能记日志，位点仍要提交
            log.error("dlq_publish_failed", msg=str(exc)[:200], rows=len(batch))

    async def _to_dlq_windows(self, windows: list[WindowResult]) -> None:
        self.counters["dlq"] += len(windows)
        log.error("window_write_failed", msg="aggregates dropped to DLQ counter", rows=len(windows))

    # ------------------------------------------------------------------ 窗口兜底

    async def _window_tick_loop(self) -> None:
        """设备全部停报时，事件时间不再推进，最后一个窗口会永远开着。

        以墙钟兜底关闭（``now - 60 s`` 作为水位线）。这是**兜底**而非主路径：
        正常情况下水位线由事件时间推进，墙钟只处理"没有新数据"的边界。
        """
        while not self._stop.is_set():
            await asyncio.sleep(self._settings.window_size_s / 4)
            mark = to_millis(utc_now()) - self._settings.watermark_delay_s * 1000
            closed = self._windows.advance(mark)
            if not closed:
                continue
            rows = await self._with_retry("insert_windows_tick", lambda: self._writer.insert_windows(closed))
            if rows is None:
                await self._to_dlq_windows(closed)
            else:
                self.counters["windows_closed"] += len(closed)
                self.counters["window_rows"] += rows


def main() -> int:
    parser = argparse.ArgumentParser(description="CDCIM stream compute (telemetry -> TDengine)")
    parser.add_argument("--once", action="store_true", help="处理一批后退出")
    parser.add_argument("--timeout", type=float, default=10.0, help="--once 的等待上限（秒）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印 SQL 不落库（本地无 TDengine 时冒烟）"
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.dry_run:
        settings = settings.model_copy(update={"tdengine_dry_run": True})
    configure_logging(SERVICE, settings.log_level, json_output=settings.log_json)
    log.info("stream_starting", msg="bootstrap", **settings.safe_summary())

    with trace_scope(new_trace_id()):
        worker = StreamWorker(settings, TDengineWriter(settings))
        return asyncio.run(serve_with_health(worker.run(once=args.once, once_timeout_s=args.timeout), port=None if args.once else 8002))


if __name__ == "__main__":
    raise SystemExit(main())
