"""60 s 滚动窗口聚合（骨架）。

**聚合函数必须独立可复用**（ADR-0007「简化流式 + 存储层重算」）：
在线路径（本模块的 :func:`aggregate`）与重算路径（``POST /maintenance/recompute``）
调用**同一个函数**。这是"明细与聚合口径一致、误差只剩浮点求和顺序"的全部依据；
若重算另写一份 SQL 聚合，两套口径迟早漂移，而漂移的症状是"历史曲线和日报对不上"，
很难归因到某次发版。

窗口语义（与《数据架构》§3.5 一致）：

* **滚动（tumbling）60 s**，窗口边界按 UTC 毫秒对齐到 ``window_size_s`` 的整数倍，
  不随进程启动时刻漂移；
* **水位线 = max_event_ts − 60 s**，窗口 ``[start, end)`` 满足 ``end ≤ watermark`` 才关闭；
* **缺失不插值**：``valid_count < sample_count`` 只体现在 ``quality_flag`` 与计数上，
  ``avg`` 只对有效点求均值——插值会把"设备离线 3 分钟"抹成"运行平稳"；
* **迟到样本不进已关闭窗口**：``ts < watermark`` 的样本计入 ``late_count``，
  由调用方转 ``telemetry.late``（契约 §2 第 3 行），不修正已发布聚合。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from cdcim_shared.contracts import TelemetrySample
from cdcim_shared.timeutil import to_millis

__all__ = [
    "AggregateValue",
    "QUALITY_FLAG_BAD",
    "QUALITY_FLAG_GOOD",
    "QUALITY_FLAG_PARTIAL",
    "TumblingWindowAggregator",
    "WindowResult",
    "aggregate",
]

#: ``quality_flag`` 取值（P4 约定，P5 与数据字典 E14 的 TINYINT 编码对齐后回写）。
QUALITY_FLAG_GOOD = 0      # 全部样本有效
QUALITY_FLAG_PARTIAL = 1   # 部分样本无效或缺失
QUALITY_FLAG_BAD = 2       # 全部无效

_VALID_QUALITIES = frozenset({"good", "uncertain"})


@dataclass(frozen=True)
class AggregateValue:
    """一个窗口的聚合结果。字段与 ``dcim_1m.st_metric_1m`` 的列一一对应。"""

    val_avg: float | None
    val_max: float | None
    val_min: float | None
    val_first: float | None
    val_last: float | None
    sample_count: int
    valid_count: int
    quality_flag: int


def aggregate(samples: Sequence[TelemetrySample]) -> AggregateValue | None:
    """对**同一测点、同一窗口内**的样本求 avg/min/max/first/last。

    返回值 ``None`` 表示窗口内没有任何样本（调用方不应写空窗口）。
    纯函数、无 IO、无状态——重算任务可直接对历史查询结果复用它。
    """
    if not samples:
        return None

    numeric: list[tuple[int, float]] = []
    for sample in samples:
        if sample.quality not in _VALID_QUALITIES:
            continue
        value = sample.value
        if isinstance(value, bool) or value is None:
            # 遥信（bool）不做数值聚合；``bool`` 是 ``int`` 的子类，必须先判断
            continue
        numeric.append((to_millis(sample.ts), float(value)))

    sample_count = len(samples)
    if not numeric:
        return AggregateValue(
            val_avg=None,
            val_max=None,
            val_min=None,
            val_first=None,
            val_last=None,
            sample_count=sample_count,
            valid_count=0,
            quality_flag=QUALITY_FLAG_BAD,
        )

    numeric.sort(key=lambda item: item[0])      # 按 ts 排序保证 first/last 语义
    values = [value for _, value in numeric]
    total = 0.0
    for value in values:
        total += value

    return AggregateValue(
        val_avg=round(total / len(values), 6),
        val_max=max(values),
        val_min=min(values),
        val_first=values[0],
        val_last=values[-1],
        sample_count=sample_count,
        valid_count=len(values),
        quality_flag=(
            QUALITY_FLAG_GOOD if len(values) == sample_count else QUALITY_FLAG_PARTIAL
        ),
    )


@dataclass(frozen=True)
class WindowResult:
    """待写入 ``dcim_1m.m_<point_id>`` 的一行。``window_start`` 即该行 ``ts``。"""

    point_id: int
    device_id: int
    window_start_ms: int
    window_end_ms: int
    agg: AggregateValue

    @property
    def granularity(self) -> str:
        return "1m"


@dataclass
class _WindowState:
    point_id: int
    device_id: int
    start_ms: int
    samples: list[TelemetrySample] = field(default_factory=list)


class TumblingWindowAggregator:
    """按测点维护当前窗口；``advance(watermark)`` 关闭窗口并产出结果。

    单进程、单协程使用（``cdcim-stream`` 是 1 worker，见进程视图 §3.2），
    故不加锁。若 P5 改多实例，同一 ``point_id`` 仍只落一个分区、只被一个实例处理，
    键空间 ``cdcim:win:{device_id}:{point_id}`` 不变即正确（契约 §5.3 连带影响 3）。
    """

    def __init__(self, window_size_s: int = 60) -> None:
        self._window_ms = window_size_s * 1000
        self._states: dict[int, _WindowState] = {}
        self._max_event_ms = 0
        self.late_count = 0
        self.closed_count = 0

    @property
    def watermark_ms(self) -> int:
        """水位线：已见最大事件时间 − 一个窗口。无数据时为 0。"""
        if self._max_event_ms == 0:
            return 0
        return self._max_event_ms - self._window_ms

    def _align(self, ts_ms: int) -> int:
        return ts_ms - (ts_ms % self._window_ms)

    def add(self, sample: TelemetrySample) -> list[WindowResult]:
        """加入一条样本。返回**因推进水位线而关闭**的窗口结果（可能为空）。"""
        ts_ms = to_millis(sample.ts)
        if ts_ms < self.watermark_ms:
            # 迟到：不进入窗口，交由调用方写 telemetry.late
            self.late_count += 1
            self._max_event_ms = max(self._max_event_ms, ts_ms)
            return []

        self._max_event_ms = max(self._max_event_ms, ts_ms)
        start_ms = self._align(ts_ms)
        state = self._states.get(sample.point_id)
        if state is None or state.start_ms != start_ms:
            flushed: list[WindowResult] = []
            if state is not None:
                flushed.append(self._close(state))
            self._states[sample.point_id] = _WindowState(
                point_id=sample.point_id,
                device_id=sample.device_id,
                start_ms=start_ms,
                samples=[sample],
            )
            return [*flushed, *self.advance()]
        state.samples.append(sample)
        return self.advance()

    def advance(self, watermark_ms: int | None = None) -> list[WindowResult]:
        """关闭所有 ``end ≤ watermark`` 的窗口。进程空闲时也应由定时协程调用，
        否则最后一个窗口永远等不到下一条样本而无法封闭。"""
        mark = self.watermark_ms if watermark_ms is None else watermark_ms
        if mark <= 0:
            return []
        closed: list[WindowResult] = []
        for point_id in list(self._states):
            state = self._states[point_id]
            if state.start_ms + self._window_ms <= mark:
                closed.append(self._close(state))
                del self._states[point_id]
        return closed

    def _close(self, state: _WindowState) -> WindowResult:
        agg = aggregate(state.samples)
        if agg is None:
            raise ValueError("window state must not be empty")
        self.closed_count += 1
        return WindowResult(
            point_id=state.point_id,
            device_id=state.device_id,
            window_start_ms=state.start_ms,
            window_end_ms=state.start_ms + self._window_ms,
            agg=agg,
        )

    def pending_windows(self) -> int:
        return len(self._states)

    def ingest_many(self, samples: Iterable[TelemetrySample]) -> list[WindowResult]:
        results: list[WindowResult] = []
        for sample in samples:
            results.extend(self.add(sample))
        return results
