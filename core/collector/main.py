"""采集网关：MQTT 上行 → 事件总线（``telemetry.raw``），断连时落本地队列。

链路（时序图见《CDCIM-P4-DD-01》§1）::

    edge device --MQTT--> cdcim-mqtt --cdcim/+/+/telemetry--> [本进程]
        ├─ 在线：解析 → TelemetrySample → Kafka telemetry.raw（key=device_code）
        └─ 断连/投递失败：SQLite 队列落盘 → 恢复后按事件时间 → telemetry.replay

四条实现约束（写法上必须能一眼看出来）：

1. **``ts`` 用原始采样时刻，不做任何偏移**（D-31 / FR-M1-06）。解析时只补时区、
   截断到毫秒；``occurred_at`` 直接取 ``ts``，不用"收到报文的时刻"。
2. **一条 MQTT 报文只承载一条样本**（契约 §6 硬约束）。收到数组即整条拒收并计数——
   打包会让幂等键变成数组、一条坏数据带坏整包。
3. **MQTT 回调线程不碰 asyncio 状态**：只做 ``call_soon_threadsafe`` 投递，
   解析与写总线都在事件循环内完成（进程视图 §3.2：1 事件循环 + 有界线程池）。
4. **补传走独立主题且限速**（D-41）：与在线流量分主题，速率 ≤ 1 440 条·秒⁻¹，
   避免恢复瞬间的补传洪峰把在线链路 P99 推高。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 入口脚本直接运行时（python core/collector/main.py）把 shared 挂进 sys.path；
# 容器内则由 PYTHONPATH=/app/shared 提供，此分支不生效。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

import argparse
import asyncio
import json
import signal
from datetime import datetime
from typing import Any

import paho.mqtt.client as mqtt
from aiokafka import AIOKafkaProducer

from buffer import DiskBuffer, EnqueueResult
from cdcim_shared.config import Settings, get_settings
from cdcim_shared.contracts import TelemetrySample
from cdcim_shared.logging import configure_logging, get_logger, new_trace_id, trace_scope
from cdcim_shared.timeutil import normalize_ms, utc_now
from cdcim_shared.topics import Topic

SERVICE = "collector-gateway"
QUEUE_MAXSIZE = 10_000          # 队列容量：400 点·秒⁻¹ 下约 25 s 的缓冲
ONLINE_BATCH_MAX = 200          # 一轮最多取多少条一起发（Kafka 侧还有 linger 批）
ONLINE_BATCH_DELAY_S = 0.05     # 攒批上限 50 ms，远小于 A-7 的 100 ms 预算
STATS_INTERVAL_S = 30.0

log = get_logger(__name__)


class Collector:
    """一个 MQTT 连接 + 一个 Kafka 生产者 + 一个磁盘队列。"""

    def __init__(self, settings: Settings, buffer: DiskBuffer) -> None:
        self._settings = settings
        self._buffer = buffer
        self._queue: asyncio.Queue[TelemetrySample] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._producer: AIOKafkaProducer | None = None
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._first_published = asyncio.Event()

        # 计数仅用于日志与 /readyz 语义，不做持久化（P6 接 Prometheus）
        self.counters: dict[str, int] = {
            "received": 0,
            "published": 0,
            "buffered": 0,
            "replayed": 0,
            "parse_error": 0,
            "queue_drop": 0,
            "buffer_overflow": 0,
            "publish_error": 0,
        }

    # ------------------------------------------------------------------ 启动 / 停止

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            acks=self._settings.kafka_acks,
            linger_ms=self._settings.kafka_linger_ms,
            compression_type=self._settings.kafka_compression,
            max_request_size=self._settings.kafka_max_message_bytes * 1024,
        )
        await self._producer.start()
        self._start_mqtt()

    def _start_mqtt(self) -> None:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{self._settings.mqtt_client_id}-{SERVICE}",
            protocol=mqtt.MQTTv311,
        )
        if self._settings.mqtt_username:
            client.username_pw_set(
                self._settings.mqtt_username,
                self._settings.mqtt_password.get_secret_value()
                if self._settings.mqtt_password
                else None,
            )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect(
            self._settings.mqtt_host,
            self._settings.mqtt_port,
            keepalive=self._settings.mqtt_keepalive_s,
        )
        client.loop_start()     # 网络线程；回调里只做线程安全投递
        self._client = client

    async def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
        if self._producer is not None:
            await self._producer.stop()

    # ------------------------------------------------------------------ MQTT 回调（网络线程）

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        if reason_code != 0:
            log.error("mqtt_connect_failed", msg="broker rejected connection", rc=str(reason_code))
            return
        client.subscribe(self._settings.mqtt_topic_pattern, qos=self._settings.mqtt_qos)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._connected.set)
        log.info(
            "mqtt_connected",
            msg="subscribed telemetry topic",
            pattern=self._settings.mqtt_topic_pattern,
            qos=self._settings.mqtt_qos,
        )

    def _on_disconnect(
        self, _client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _p: Any = None
    ) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._connected.clear)
        log.warning("mqtt_disconnected", msg="link down, samples will be buffered", rc=str(reason_code))

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        """网络线程 → 事件循环。**只做搬运**，异常必须就地吞掉并计数，
        否则 paho 的网络线程会因未捕获异常而中断收包。"""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._handle_message, message.topic, message.payload)
        except RuntimeError:      # 事件循环已关闭（进程退出中），丢弃即可
            return

    # ------------------------------------------------------------------ 事件循环内：解析与入队

    def _handle_message(self, topic: str, payload: bytes) -> None:
        self.counters["received"] += 1
        sample = self._parse(topic, payload)
        if sample is None:
            return
        try:
            self._queue.put_nowait(sample)
        except asyncio.QueueFull:
            # 背压手段之一：丢最新并计数。丢最旧会让"卡了多久"不可观测。
            self.counters["queue_drop"] += 1
            log.warning("queue_full_drop", msg="in-memory queue full, sample dropped", point_id=sample.point_id)

    def _parse(self, topic: str, payload: bytes) -> TelemetrySample | None:
        """``cdcim/<site_code>/<device_code>/telemetry`` + 单对象 JSON。

        载荷字段（P4 约定的适配层契约，P5 由设备模板驱动生成）::

            {"device_id":33,"point_id":1007,"value":221.35,"value_type":"double",
             "ts":"2026-11-27T10:00:03.120+08:00","quality":"good",
             "raw_value":22135,"source_batch":"b20261127-1000"}

        ``device_code`` 取自**主题**而非载荷：主题里的末端设备编码是分区键来源，
        允许载荷自报会让"上报网关代填"悄悄破坏分区键（契约 §5.2 情形 B）。
        """
        parts = topic.split("/")
        if len(parts) != 4 or parts[3] != "telemetry":
            self.counters["parse_error"] += 1
            log.warning("topic_unexpected", msg="topic does not match pattern", topic=topic)
            return None
        device_code = parts[2]

        try:
            body = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.counters["parse_error"] += 1
            log.warning("payload_not_json", msg=str(exc), topic=topic)
            return None
        if not isinstance(body, dict):
            # 契约 §6：禁止把多条样本打包成一条消息
            self.counters["parse_error"] += 1
            log.warning("payload_not_object", msg="batched samples are not allowed", topic=topic)
            return None

        try:
            ts_raw = body["ts"]
            sample_ts = normalize_ms(datetime.fromisoformat(str(ts_raw)))
            sample = TelemetrySample.build(
                producer=f"{SERVICE}/edge",
                trace_id=new_trace_id(),
                device_id=int(body["device_id"]),
                device_code=device_code,
                point_id=int(body["point_id"]),
                point_code=body.get("point_code"),
                ts=sample_ts,
                value=body.get("value"),
                value_type=body.get("value_type", _infer_value_type(body.get("value"))),
                quality=body.get("quality", "good"),
                ts_source=body.get("ts_source", "device"),
                raw_value=body.get("raw_value"),
                source_batch=body.get("source_batch"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # KeyError 覆盖缺字段；ValueError 覆盖 naive 时间与越界枚举
            self.counters["parse_error"] += 1
            log.warning("payload_invalid", msg=str(exc), topic=topic)
            return None
        return sample

    # ------------------------------------------------------------------ 发布协程

    async def publisher_loop(self) -> None:
        """在线发布：批量取出 → 逐条 send（Kafka 侧再按 linger 成批）→ 失败即落盘。"""
        assert self._producer is not None
        while not self._stop.is_set():
            batch = await self._take_batch()
            if not batch:
                continue
            sent = 0
            for sample in batch:
                if await self._send(self._producer, Topic.TELEMETRY_RAW, sample):
                    sent += 1
                else:
                    await self._buffer_sample(sample)
            self.counters["published"] += sent
            if sent:
                self._first_published.set()

    async def _take_batch(self) -> list[TelemetrySample]:
        batch: list[TelemetrySample] = []
        # 先等第一条（阻塞），再在 50 ms 内尽量多取——避免空转
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return batch
        batch.append(first)
        deadline = asyncio.get_running_loop().time() + ONLINE_BATCH_DELAY_S
        while len(batch) < ONLINE_BATCH_MAX:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
            except asyncio.TimeoutError:
                break
        return batch

    async def _send(
        self, producer: AIOKafkaProducer, topic: str, sample: TelemetrySample
    ) -> bool:
        try:
            await producer.send_and_wait(
                topic,
                key=sample.partition_key.encode(),
                value=sample.model_dump_json().encode(),
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 总线故障面未知，统一转入本地缓冲
            self.counters["publish_error"] += 1
            log.error(
                "kafka_send_failed",
                msg="publish failed, falling back to disk buffer",
                topic=topic,
                error=type(exc).__name__,
                point_id=sample.point_id,
            )
            return False

    async def _buffer_sample(self, sample: TelemetrySample) -> None:
        """落盘必须在协程外执行（PY-06：SQLite 的 fsync 是同步阻塞调用）。"""
        result, warn = await asyncio.to_thread(self._enqueue_sync, sample)
        if result.overflow:
            self.counters["buffer_overflow"] += 1
            log.error(
                "buffer_overflow",
                msg="disk buffer quota exhausted, sample rejected",
                quota_mb=self._settings.buffer_quota_mb,
                rows=result.rows,
            )
            return
        self.counters["buffered"] += 1
        if warn:
            log.warning(
                "buffer_high_watermark",
                msg="disk buffer above 80% of quota",
                rows=result.rows,
                usage_ratio=round(result.usage_ratio, 3),
            )

    def _enqueue_sync(self, sample: TelemetrySample) -> tuple[EnqueueResult, bool]:
        result = self._buffer.enqueue(sample)
        return result, self._buffer.should_warn()

    # ------------------------------------------------------------------ 补传协程

    async def replay_loop(self) -> None:
        """按事件时间补传，独立主题 + 令牌桶限速（D-41）。"""
        assert self._producer is not None
        interval = 1.0 / max(1, self._settings.buffer_replay_rate_per_s)
        while not self._stop.is_set():
            chunks = await asyncio.to_thread(
                self._buffer.peek, self._settings.buffer_replay_batch
            )
            if not chunks:
                await asyncio.sleep(1.0)
                continue
            acked: list[int] = []
            for seq, sample in chunks:
                if self._stop.is_set():
                    break
                replay_sample = sample.model_copy(
                    update={"ts_source": "replay", "replay": True, "produced_at": utc_now()}
                )
                if not await self._send(self._producer, Topic.TELEMETRY_REPLAY, replay_sample):
                    break     # 仍是断连：保留队列，下一轮再试
                acked.append(seq)
                self.counters["replayed"] += 1
                await asyncio.sleep(interval)
            if acked:
                await asyncio.to_thread(self._buffer.ack, acked)
                log.info("replay_flushed", msg="buffered samples replayed", rows=len(acked))

    # ------------------------------------------------------------------ 观测

    async def stats_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(STATS_INTERVAL_S)
            stats = await asyncio.to_thread(self._buffer.stats)
            log.info(
                "collector_stats",
                msg="periodic counters",
                queue_depth=self._queue.qsize(),
                buffer_rows=stats.rows,
                buffer_usage=round(stats.usage_ratio, 4),
                **self.counters,
            )

    # ------------------------------------------------------------------ 入口

    async def run(self, *, once: bool = False, once_timeout_s: float = 20.0) -> int:
        await self.start()
        tasks = [
            asyncio.create_task(self.publisher_loop(), name="publisher"),
            asyncio.create_task(self.replay_loop(), name="replay"),
        ]
        if not once:
            tasks.append(asyncio.create_task(self.stats_loop(), name="stats"))

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=once_timeout_s)
        except asyncio.TimeoutError:
            log.error("mqtt_connect_timeout", msg="cannot reach broker", host=self._settings.mqtt_host)
            await self.stop()
            return 2

        if once:
            try:
                await asyncio.wait_for(self._first_published.wait(), timeout=once_timeout_s)
            except asyncio.TimeoutError:
                log.warning("once_no_sample", msg="no telemetry received within timeout")
            await self.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("once_done", msg="single-shot run finished", **self.counters)
            return 0

        await self._install_signal_handlers()
        await self._stop.wait()
        await self.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stats = await asyncio.to_thread(self._buffer.stats)
        log.info("collector_stopped", msg="graceful shutdown", buffer_rows=stats.rows, **self.counters)
        return 0

    async def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                # Windows 不支持 add_signal_handler：退回 KeyboardInterrupt 路径
                continue


def _infer_value_type(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    return "double"


def _resolve_buffer_path(settings: Settings) -> Path:
    """容器内用 ``/var/lib/cdcim/buffer``；本地（Windows/无该目录写权限）退回工作目录。

    这是**开发便利性**的显式降级，不改变生产行为：路径不可写时打一条 warning，
    绝不静默改用内存队列（那会让"重启不丢"的承诺失效）。
    """
    target = settings.buffer_path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        fallback = Path.cwd() / ".cdcim" / "buffer" / target.name
        log.warning("buffer_path_fallback", msg="default buffer path not writable", path=str(fallback))
        return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description="CDCIM collector gateway (MQTT -> Kafka)")
    parser.add_argument("--once", action="store_true", help="跑一轮（收到并发布一条样本）后退出")
    parser.add_argument("--timeout", type=float, default=20.0, help="--once 的等待上限（秒）")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(SERVICE, settings.log_level, json_output=settings.log_json)
    log.info("collector_starting", msg="bootstrap", **settings.safe_summary())

    with trace_scope(new_trace_id()):
        buffer = DiskBuffer(
            _resolve_buffer_path(settings),
            settings.buffer_quota_bytes,
            high_watermark=settings.buffer_high_watermark,
        )
        buffer.open()
        try:
            return asyncio.run(Collector(settings, buffer).run(once=args.once, once_timeout_s=args.timeout))
        finally:
            buffer.close()


if __name__ == "__main__":
    raise SystemExit(main())
