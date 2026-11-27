"""窗口聚合：时间对齐与水位线关闭。"""
from __future__ import annotations

from datetime import timedelta

from cdcim_shared.contracts import TelemetrySample
from cdcim_shared.timeutil import normalize_ms, utc_now

from window import TumblingWindowAggregator


def _sample(ts, value: float) -> TelemetrySample:
    return TelemetrySample.build(
        producer="test", trace_id="win000000000001", device_id=1,
        device_code="UPS-A-01", point_id=7001, ts=ts,
        value=value, value_type="double", quality="good", ts_source="device",
    )


def test_window_closes_on_watermark() -> None:
    agg = TumblingWindowAggregator(window_size_s=60)
    base = normalize_ms(utc_now()).replace(second=0, microsecond=0)
    agg.add(_sample(base + timedelta(seconds=1), 20.0))
    agg.add(_sample(base + timedelta(seconds=2), 30.0))
    assert agg.pending_windows() == 1
    results = agg.advance(watermark_ms=int((base + timedelta(seconds=61)).timestamp() * 1000))
    assert len(results) == 1
    assert results[0].granularity == "1m"
    assert agg.pending_windows() == 0


def test_late_sample_after_close_is_not_reopened() -> None:
    agg = TumblingWindowAggregator(window_size_s=60)
    base = normalize_ms(utc_now()).replace(second=0, microsecond=0)
    agg.add(_sample(base + timedelta(seconds=1), 20.0))
    agg.advance(watermark_ms=int((base + timedelta(seconds=61)).timestamp() * 1000))
    agg.add(_sample(base + timedelta(seconds=2), 99.0))
    assert agg.pending_windows() == 1  # 新窗口，而不是重开旧窗口
