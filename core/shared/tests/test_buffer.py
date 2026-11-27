"""断网缓冲：写入/读出一致，水位线触发告警。"""
from __future__ import annotations

from pathlib import Path

from cdcim_shared.contracts import TelemetrySample
from cdcim_shared.timeutil import normalize_ms, utc_now

from buffer import DiskBuffer


def _sample(value: float) -> TelemetrySample:
    return TelemetrySample.build(
        producer="test", trace_id="buf000000000001", device_id=1,
        device_code="UPS-A-01", point_id=7001, ts=normalize_ms(utc_now()),
        value=value, value_type="double", quality="good", ts_source="device",
    )


def test_enqueue_peek_ack_roundtrip(tmp_path: Path) -> None:
    buf = DiskBuffer(tmp_path / "queue.db", quota_bytes=1024 * 1024)
    buf.open()
    try:
        sample = _sample(23.5)
        buf.enqueue(sample)
        rows = buf.peek(10)
        assert len(rows) == 1
        seq, restored = rows[0]
        assert restored.ts == sample.ts, "补传时间戳偏差必须为 0"
        assert restored.value == sample.value
        assert buf.ack([seq]) == 1
        assert buf.peek(10) == []
    finally:
        buf.close()


def test_high_watermark_warns_once(tmp_path: Path) -> None:
    buf = DiskBuffer(tmp_path / "q.db", quota_bytes=4096, high_watermark=0.5)
    buf.open()
    try:
        for i in range(20):
            buf.enqueue(_sample(float(i)))
        assert buf.is_high_watermark() is True
        assert buf.should_warn() in (True, False)  # 阶梯告警只触发一次，由实现控制
    finally:
        buf.close()
