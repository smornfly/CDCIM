"""配置回归：aiokafka 的 acks 只接受 int 0/1/-1 或字符串 all。"""
from __future__ import annotations

from cdcim_shared.config import Settings


def test_kafka_acks_is_accepted_by_aiokafka() -> None:
    acks = Settings().kafka_acks
    assert acks in (0, 1, -1) or acks == "all", f"非法的 acks 值: {acks!r}"
