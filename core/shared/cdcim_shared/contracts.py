"""跨服务事件契约（Pydantic v2）。**这里不放领域模型**。

边界说明：《类图与领域模型》§2 的实体/聚合（``Device``、``AlarmEvent``…）属领域层，
不跨进程；本文件只承载**过总线的东西**——即《08-消息主题与事件契约》§3 的 4 类事件
（遥测样本、告警原始事件、联动指令、AI 任务）。两者字段高度相似但职责不同：
领域对象可含行为与仓储引用，契约对象必须可 JSON 往返、无 IO、无行为。

三条实现约定：

1. **公共头内联，不嵌套 ``payload``**（契约 §3.1）：少一层解析、少一处字段路径歧义。
2. **``extra="ignore"``**：消费方必须容忍未知字段（契约 §1 原则 3、§8 兼容窗口），
   因此契约用 ignore 而非 forbid；生产侧靠 Schema Registry 的 CI 门禁兜底。
3. **序列化即契约**：``model_dump_json()`` 的输出就是总线上的字节，时间字段统一
   ``ISO 8601 + 3 位小数 + UTC 偏移``（:mod:`cdcim_shared.timeutil`）。

``schema_version`` 的当前值取自各契约的 ``$id``：遥测 1.2.0、告警 1.0.0、
联动 1.0.0、AI 1.0.0。major 变更必须换主题（契约 §8），故本模块不提供"兼容多版本"
的分支逻辑——那属于消费侧的显式适配层。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

from cdcim_shared.timeutil import new_uuid7_str, normalize_ms, to_iso_ms, utc_now

__all__ = [
    "AiTask",
    "AlarmLevel",
    "AlarmRaised",
    "EventHeader",
    "LinkageCommand",
    "LinkageStage",
    "QualityCode",
    "TelemetrySample",
    "TsSource",
    "ValueType",
]

#: 契约时间类型：入参补时区并截断到毫秒，出参固定 3 位小数（JSON 序列化时生效）。
EventTime = Annotated[
    datetime,
    PlainSerializer(to_iso_ms, return_type=str, when_used="json"),
]

QualityCode = Literal["good", "uncertain", "bad", "stale"]
ValueType = Literal["double", "int", "bool"]
TsSource = Literal["device", "gateway", "replay", "manual"]
AlarmLevel = Literal["critical", "major", "minor"]
LinkageStage = Literal[
    "requested", "confirmed", "reviewed", "dispatched", "rejected", "expired"
]


class EventHeader(BaseModel):
    """公共事件头（8 字段，契约 §3.1 表 TAB-API-12）。

    子类把 ``event_type`` 收窄为 ``Literal``，从而在类型层保证"主题与事件类型
    一一对应"。``occurred_at`` 对 ``telemetry.*`` 必须等于业务字段 ``ts``
    （契约 §3.1 末段），由 :meth:`TelemetrySample.build` 统一赋值，避免两处各写一次。
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    event_id: str = Field(default_factory=new_uuid7_str, description="UUID v7，时间有序")
    event_type: str
    schema_version: str
    occurred_at: EventTime
    produced_at: EventTime
    producer: str = Field(max_length=64, description="服务名/实例，如 collector-gateway/edge-a")
    trace_id: str = Field(max_length=32)
    partition_key: str = Field(max_length=64, description="分区键的冗余副本，供人工核对")

    @field_validator("occurred_at", "produced_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        """拒绝 naive datetime：猜时区会让同一事件在两台机器落成两个时刻。"""
        return normalize_ms(value)


class TelemetrySample(EventHeader):
    """EVT-TEL-01 ``telemetry.sample``（``telemetry.raw`` / ``telemetry.replay`` 同 schema）。

    **``ts`` 是原始采样时刻，任何链路都不得偏移**（D-31 / FR-M1-06）。本模型不对
    ``ts`` 做时区"修正"以外的加工，也不允许调用方用 ``produced_at`` 覆盖它。
    """

    event_type: Literal["telemetry.sample"] = "telemetry.sample"
    schema_version: str = "1.2.0"

    device_id: int = Field(ge=1)
    device_code: str = Field(pattern=r"^[A-Z0-9_-]{3,64}$", description="末端设备编码，分区键来源")
    point_id: int = Field(ge=1)
    point_code: str | None = Field(default=None, max_length=64)
    ts: EventTime = Field(description="事件时间（UTC 毫秒精度），同设备同测点单调不回退")
    value: float | int | bool | None
    value_type: ValueType
    quality: QualityCode
    ts_source: TsSource
    raw_value: float | None = None
    source_batch: str | None = Field(default=None, max_length=32)
    replay: bool = False
    is_late: bool = False

    @classmethod
    def build(
        cls,
        *,
        producer: str,
        trace_id: str,
        device_id: int,
        device_code: str,
        point_id: int,
        ts: datetime,
        value: float | int | bool | None,
        value_type: ValueType,
        quality: QualityCode = "good",
        ts_source: TsSource = "device",
        point_code: str | None = None,
        raw_value: float | None = None,
        source_batch: str | None = None,
        replay: bool = False,
        produced_at: datetime | None = None,
    ) -> "TelemetrySample":
        """唯一构造入口：``occurred_at = ts``，``partition_key = device_code``。

        这两条不变式若交给调用方各自填写，迟早出现"补传数据 happened_at 用了发送
        时刻"的偏差——而这类偏差在报表上表现为「延迟抖动」，很难反查到根因。
        """
        sample_ts = normalize_ms(ts)
        return cls(
            event_type="telemetry.sample",
            occurred_at=sample_ts,
            produced_at=normalize_ms(produced_at or utc_now()),
            producer=producer,
            trace_id=trace_id,
            partition_key=device_code,
            device_id=device_id,
            device_code=device_code,
            point_id=point_id,
            point_code=point_code,
            ts=sample_ts,
            value=value,
            value_type=value_type,
            quality=quality,
            ts_source=ts_source,
            raw_value=raw_value,
            source_batch=source_batch,
            replay=replay,
        )


class AlarmRaised(EventHeader):
    """EVT-ALM-01 ``alarm.raw.raised``（``alarm.raw``）。

    ``alarm_fingerprint`` 是幂等键：``hash(rule_id, device_id, point_id, window_start)``。
    指纹里放 **窗口起点**而不是发送时刻，是"一次故障只产生一条主告警"的实现基础
    （契约 §4 第 1 条）——本模块只承载字段，指纹计算属告警引擎领域服务。
    """

    event_type: Literal["alarm.raw.raised"] = "alarm.raw.raised"
    schema_version: str = "1.0.0"

    alarm_fingerprint: str = Field(min_length=16, max_length=64)
    raw_alarm_id: int | None = Field(default=None, description="落库后回填；发布前可为空")
    rule_id: int
    rule_version: int
    device_id: int
    point_id: int | None = None
    alarm_level: AlarmLevel
    alarm_type: str
    trigger_value: float | None = None
    threshold_value: float | None = None
    quality_code: QualityCode
    ts_event: EventTime
    ts_detect: EventTime
    window_start: EventTime
    is_suppressed: bool = False
    suppress_type: Literal["none", "self", "parent_child"] = "none"
    is_derived: bool = False


class LinkageCommand(EventHeader):
    """EVT-LNK-01 ``linkage.command``（``linkage.commands``）。

    ``request_id + stage`` 是幂等键；``stage`` 的六态顺序由状态机保证，消息只承载事实。
    ``action_type`` 不含"远程开门"（SRS 明确禁止），此处不做枚举收窄以免与
    《规则引擎 DSL》的五类动作脱节，由 P5 在应用层校验。
    """

    event_type: Literal["linkage.command"] = "linkage.command"
    schema_version: str = "1.0.0"

    linkage_id: int
    request_id: str
    alarm_event_id: int
    stage: LinkageStage
    action_type: str
    target_type: Literal["door", "camera", "nvr"]
    target_id: str
    action_params: dict[str, object] | None = None
    confirm_by: int | None = None
    review_by: int | None = None
    deadline_at: EventTime


class AiTask(EventHeader):
    """EVT-AI-01 ``ai.task.requested``（``ai.tasks``）。

    ``task_id = hash(alarm_fingerprint, task_type, model_version)`` 是确定性幂等键，
    命中 Redis 建议缓存即复用、不重复调用模型（契约 §4）。
    """

    event_type: Literal["ai.task.requested"] = "ai.task.requested"
    schema_version: str = "1.0.0"

    task_id: str
    alarm_event_id: int
    alarm_fingerprint: str = Field(min_length=16, max_length=64)
    task_type: Literal["root_cause", "cluster_suggest", "kb_answer"]
    deadline_at: EventTime
