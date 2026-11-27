"""契约序列化往返：总线上字节的字节数即契约（08-消息主题与事件契约 §3）。"""
from __future__ import annotations

import json
from datetime import timedelta

from cdcim_shared.contracts import TelemetrySample
from cdcim_shared.timeutil import normalize_ms, utc_now


def _sample(**overrides: object) -> TelemetrySample:
    ts = normalize_ms(utc_now())
    kwargs: dict[str, object] = dict(
        producer="test-collector",
        trace_id="0123456789abcdef",
        device_id=101,
        device_code="UPS-A-01",
        point_id=7001,
        point_code="temp_inlet",
        ts=ts,
        value=23.5,
        value_type="double",
        quality="good",
        ts_source="device",
    )
    kwargs.update(overrides)
    return TelemetrySample.build(**kwargs)  # type: ignore[arg-type]


def test_occurred_at_equals_ts_and_partition_key() -> None:
    s = _sample()
    assert s.occurred_at == s.ts, "补传/迟到的 occurred_at 必须等于原始采样时刻"
    assert s.partition_key == "UPS-A-01", "分区键是设备编码，不是测点编号"
    assert s.schema_version == "1.2.0"


def test_json_roundtrip_preserves_ts() -> None:
    s = _sample(replay=True, ts_source="replay")
    raw = s.model_dump_json()
    back = TelemetrySample.model_validate(json.loads(raw))
    assert back.ts == s.ts, "偏差必须为 0（FR-M1-06）"
    assert back.replay is True and back.ts_source == "replay"
    assert back.event_id == s.event_id


def test_replay_does_not_shift_timestamp() -> None:
    ts = normalize_ms(utc_now() - timedelta(minutes=30))
    s = _sample(ts=ts, replay=True, ts_source="replay")
    assert s.ts == ts, "D-31：任何链路不得给时间戳加偏移"
