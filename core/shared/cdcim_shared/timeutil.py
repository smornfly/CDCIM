"""时间工具：UTC 存储、毫秒精度、UUID v7。

口径（与《数据库设计说明书》§4.1 D-31、《消息主题与事件契约》§3.1 一致）：

1. **存储与传输一律 UTC**；带时区偏移的入参在解析后立即转 UTC，三处
   （事件头 ``occurred_at``、业务字段 ``ts``、日志 ``ts``）使用同一函数。
2. **精度截断到毫秒，不做时区"平移"**。``2026-11-27T10:00:03.120+08:00`` 与
   ``2026-11-27T02:00:03.120Z`` 是同一时刻，转换只改表示法；``ts`` 的**瞬时值
   一位不变**。这是 FR-M1-06「补传时间戳与原始采集时刻偏差 0」在最底层不被破坏的前提。
3. 禁止 ``datetime.now()`` 无时区调用（编码规范 PY-10），本模块只提供
   :func:`utc_now`。
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

__all__ = [
    "EPOCH",
    "MS_PER_SECOND",
    "from_millis",
    "new_uuid7_str",
    "normalize_ms",
    "to_iso_ms",
    "to_millis",
    "utc_now",
]

MS_PER_SECOND = 1000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_now() -> datetime:
    """当前时刻（UTC、毫秒精度）。全仓唯一允许的"取当前时间"入口。"""
    return normalize_ms(datetime.now(timezone.utc))


def normalize_ms(moment: datetime) -> datetime:
    """补齐时区并截断到毫秒。

    无时区的 ``datetime`` 视为编程错误直接拒绝——猜时区会让同一条数据在不同
    机器上落成两个时刻，属于最难复现的一类事故（编码规范 PY-10）。
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("naive datetime is not allowed; attach a timezone explicitly")
    utc = moment.astimezone(timezone.utc)
    # 只截断微秒位（1 ms = 1000 us），不改变瞬时秒值
    micros = utc.microsecond - (utc.microsecond % 1000)
    return utc.replace(microsecond=micros)


def to_iso_ms(moment: datetime) -> str:
    """ISO 8601 + 固定 3 位小数 + ``+00:00``，例如 ``2026-11-27T02:00:03.120+00:00``。"""
    return normalize_ms(moment).isoformat(timespec="milliseconds")


def to_millis(moment: datetime) -> int:
    """UTC 毫秒时间戳。TDengine ``TIMESTAMP`` 与缓冲队列都用这个整数排序。"""
    delta = normalize_ms(moment) - EPOCH
    return delta.days * 86_400_000 + delta.seconds * MS_PER_SECOND + delta.microseconds // 1000


def from_millis(epoch_millis: int) -> datetime:
    """UTC 毫秒时间戳 → ``datetime``。"""
    return EPOCH + timedelta(milliseconds=epoch_millis)


def new_uuid7_str() -> str:
    """RFC 9562 UUID v7（48 bit 毫秒时间 + 版本位 + 随机尾）。

    消息契约要求 ``event_id`` 时间有序，便于按 ID 排序与补推；标准库 3.11
    尚无 ``uuid7``，此处按规范手工拼装，避免为一个 ID 引入依赖。
    """
    epoch_ms = int(time.time() * MS_PER_SECOND) & ((1 << 48) - 1)
    random_bits = int.from_bytes(os.urandom(10), "big")
    # 低 80 bit：12 bit rand_a + 62 bit rand_b（version=7, variant=0b10）
    rand_a = (random_bits >> 68) & 0x0FFF
    rand_b = random_bits & ((1 << 62) - 1)
    value = (
        (epoch_ms << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return str(uuid.UUID(int=value))
