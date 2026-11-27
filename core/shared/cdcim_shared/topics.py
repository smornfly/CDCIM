"""13 个主题名、消费组、事件类型常量。

唯一口径来源：《08-消息主题与事件契约》§2 表 TAB-API-11。**其余代码不得再写字面量
主题名**——分区数、分区键、保留期都依附于这个名字，散落的字符串会让「改主题只改一处」
失效（8 个服务、3 个 Dockerfile 里的 topic 拼错一个，症状是"没数据但不报错"）。

分区数一并声明只是为了在文档与代码中做一致性断言（P4 不创建主题，创建脚本属
P4 部署文件、由另一位同事落地）；本文件不读 broker 元数据。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ALL_TOPICS",
    "CONSUMER_GROUPS",
    "ConsumerGroup",
    "EventType",
    "REPLAY_MAX_RATE_PER_SECOND",
    "TOPIC_PARTITIONS",
    "Topic",
]

# --------------------------------------------------------------------------
# 主题（13 个）
# --------------------------------------------------------------------------


class Topic:
    """主题名常量。用类属性而非模块级常量，便于 ``for name in ALL_TOPICS`` 枚举。"""

    TELEMETRY_RAW: Final[str] = "telemetry.raw"
    TELEMETRY_REPLAY: Final[str] = "telemetry.replay"
    TELEMETRY_LATE: Final[str] = "telemetry.late"
    METRIC_WINDOW: Final[str] = "metric.window"
    ALARM_RAW: Final[str] = "alarm.raw"
    ALARM_EVENTS: Final[str] = "alarm.events"
    LINKAGE_COMMANDS: Final[str] = "linkage.commands"
    LINKAGE_RESULTS: Final[str] = "linkage.results"
    AI_TASKS: Final[str] = "ai.tasks"
    AI_RESULTS: Final[str] = "ai.results"
    META_CHANGE: Final[str] = "meta.change"
    NOTIFY_OUTBOX: Final[str] = "notify.outbox"
    DLQ_DEFAULT: Final[str] = "dlq.default"


ALL_TOPICS: Final[tuple[str, ...]] = (
    Topic.TELEMETRY_RAW,
    Topic.TELEMETRY_REPLAY,
    Topic.TELEMETRY_LATE,
    Topic.METRIC_WINDOW,
    Topic.ALARM_RAW,
    Topic.ALARM_EVENTS,
    Topic.LINKAGE_COMMANDS,
    Topic.LINKAGE_RESULTS,
    Topic.AI_TASKS,
    Topic.AI_RESULTS,
    Topic.META_CHANGE,
    Topic.NOTIFY_OUTBOX,
    Topic.DLQ_DEFAULT,
)

#: 契约 §2 的分区数与设计一致；``telemetry.raw`` 恒为 1（§5.1 硬约束，D-41 的推论）。
TOPIC_PARTITIONS: Final[dict[str, int]] = {
    Topic.TELEMETRY_RAW: 1,
    Topic.TELEMETRY_REPLAY: 1,
    Topic.TELEMETRY_LATE: 3,
    Topic.METRIC_WINDOW: 6,
    Topic.ALARM_RAW: 3,
    Topic.ALARM_EVENTS: 3,
    Topic.LINKAGE_COMMANDS: 1,
    Topic.LINKAGE_RESULTS: 1,
    Topic.AI_TASKS: 2,
    Topic.AI_RESULTS: 2,
    Topic.META_CHANGE: 1,
    Topic.NOTIFY_OUTBOX: 3,
    Topic.DLQ_DEFAULT: 1,
}

#: 补传限速上限（契约 §2 第 2 行）：1 440 条·秒⁻¹，5 min 补完 30 min 数据。
REPLAY_MAX_RATE_PER_SECOND: Final[int] = 1440


# --------------------------------------------------------------------------
# 消费组
# --------------------------------------------------------------------------


class ConsumerGroup:
    """消费组名。采集/流计算/告警各自持有独立位点，互不阻塞。"""

    COMPUTE: Final[str] = "cg-compute"
    COMPUTE_REPLAY: Final[str] = "cg-compute-replay"
    ALARM: Final[str] = "cg-alarm"
    QUALITY: Final[str] = "cg-quality"
    API: Final[str] = "cg-api"
    ROLLUP: Final[str] = "cg-rollup"
    ENERGY: Final[str] = "cg-energy"


CONSUMER_GROUPS: Final[tuple[str, ...]] = tuple(
    value
    for name, value in vars(ConsumerGroup).items()
    if name.isupper() and isinstance(value, str)
)


# --------------------------------------------------------------------------
# 事件类型（与主题一一对应，见契约 §3.2 ~ §3.13）
# --------------------------------------------------------------------------


class EventType:
    """``event_type`` 取值。生产侧写常量，消费侧按常量分支。"""

    TELEMETRY_SAMPLE: Final[str] = "telemetry.sample"
    TELEMETRY_LATE_DETECTED: Final[str] = "telemetry.late_detected"
    METRIC_WINDOW_CLOSED: Final[str] = "metric.window.closed"
    ALARM_RAW_RAISED: Final[str] = "alarm.raw.raised"
    ALARM_EVENT_CHANGED: Final[str] = "alarm.event.changed"
    LINKAGE_COMMAND: Final[str] = "linkage.command"
    LINKAGE_RESULT: Final[str] = "linkage.result"
    AI_TASK_REQUESTED: Final[str] = "ai.task.requested"
    AI_ADVICE_READY: Final[str] = "ai.advice.ready"
    META_CHANGED: Final[str] = "meta.changed"
    NOTIFY_OUTBOX: Final[str] = "notify.outbox"
    DEAD_LETTER: Final[str] = "dead.letter"


#: 主题 → 该主题唯一合法 ``event_type``。消费侧据此快速拒绝"串主题"的消息。
TOPIC_EVENT_TYPE: Final[dict[str, str]] = {
    Topic.TELEMETRY_RAW: EventType.TELEMETRY_SAMPLE,
    Topic.TELEMETRY_REPLAY: EventType.TELEMETRY_SAMPLE,
    Topic.TELEMETRY_LATE: EventType.TELEMETRY_LATE_DETECTED,
    Topic.METRIC_WINDOW: EventType.METRIC_WINDOW_CLOSED,
    Topic.ALARM_RAW: EventType.ALARM_RAW_RAISED,
    Topic.ALARM_EVENTS: EventType.ALARM_EVENT_CHANGED,
    Topic.LINKAGE_COMMANDS: EventType.LINKAGE_COMMAND,
    Topic.LINKAGE_RESULTS: EventType.LINKAGE_RESULT,
    Topic.AI_TASKS: EventType.AI_TASK_REQUESTED,
    Topic.AI_RESULTS: EventType.AI_ADVICE_READY,
    Topic.META_CHANGE: EventType.META_CHANGED,
    Topic.NOTIFY_OUTBOX: EventType.NOTIFY_OUTBOX,
    Topic.DLQ_DEFAULT: EventType.DEAD_LETTER,
}
