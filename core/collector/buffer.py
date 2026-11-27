"""断网本地缓冲：SQLite 持久队列（配额可配，80% 水位告警）。

设计要点与依据：

* **落盘而不是内存队列**：边端进程重启（Docker ``restart: unless-stopped``）后内存队列
  必然清空，而 D-41 承诺"补传保证明细 100%"。SQLite 单文件 + 同步 ``commit`` 是
  在 256 MB 内存预算内唯一能兑现该承诺的选择；为此接受"每秒若干次 fsync"的代价，
  且 fsync 必须在调用方经 ``asyncio.to_thread`` 执行（编码规范 PY-06）。
* **按事件时间出队**：补传顺序必须与原始采样顺序一致，否则 TDengine 同 ``ts`` 覆盖
  会退化成"后来者赢"，历史值被旧值改写。故出队以 ``(ts_ms, seq)`` 排序，
  而不是 ``AUTOINCREMENT`` 顺序——网络抖动时入队顺序本就可能乱。
* **配额是硬上限**：超过 150 MB 时**拒收新样本并计数**（而非静默丢最旧）。
  丢最旧会让"断网 40 min"与"断网 4 h"的表现一致，运维无法判断损失范围；
  拒收则留下明确的 ``buffer_overflow`` 计数，可对账。
* **80% 水位只打日志**：提前一个量级给出预警（150 MB × 0.8 = 120 MB），
  对应约 8 h 的稳态积压（设计目标，实测在 P6），足够值班员处置。

线程模型：本类全部方法为**同步**，由调用方放进线程池；类内用 ``threading.Lock``
串行化写，读允许并发（SQLite WAL 由 ``journal_mode`` 控制，这里保持默认 rollback
journal 以兼容只读挂载与备份）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cdcim_shared.contracts import TelemetrySample
from cdcim_shared.timeutil import to_millis

__all__ = ["BufferStats", "DiskBuffer", "EnqueueResult"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_queue (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT    NOT NULL UNIQUE,
    ts_ms        INTEGER NOT NULL,
    enqueued_ms  INTEGER NOT NULL,
    payload      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replay_order ON replay_queue (ts_ms, seq);
"""


@dataclass(frozen=True)
class BufferStats:
    """一次采样的缓冲状态。字段名直接对应日志与健康检查输出。"""

    rows: int
    bytes_used: int
    quota_bytes: int
    oldest_ts_ms: int | None
    newest_ts_ms: int | None

    @property
    def usage_ratio(self) -> float:
        if self.quota_bytes <= 0:
            return 1.0
        return self.bytes_used / self.quota_bytes

    @property
    def is_high_watermark(self) -> bool:
        return self.usage_ratio >= 0.8


@dataclass(frozen=True)
class EnqueueResult:
    """入队结果。``overflow=True`` 表示配额已满、该条样本被拒收。"""

    accepted: bool
    rows: int
    usage_ratio: float
    overflow: bool


class DiskBuffer:
    """补传队列。一个采集进程一个实例，**不要跨进程共享**（SQLite 写锁粒度是库）。"""

    def __init__(
        self,
        path: Path,
        quota_bytes: int,
        *,
        high_watermark: float = 0.8,
        warn_ratio_step: float = 0.1,
    ) -> None:
        self._path = path
        self._quota_bytes = quota_bytes
        self._high_watermark = high_watermark
        # 水位告警按 10% 阶梯触发，避免 80%~100% 区间每来一条都刷日志
        self._warn_ratio_step = warn_ratio_step
        self._last_warned_ratio = 0.0
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ 生命周期

    def open(self) -> None:
        """建库建表。目录不存在则创建（挂载卷首次启动的场景）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=10.0, check_same_thread=False)
        conn.execute("PRAGMA synchronous=FULL")   # 断网期就是要"落盘即持久"
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DiskBuffer is not open; call open() first")
        return self._conn

    # ------------------------------------------------------------------ 写入

    def enqueue(self, sample: TelemetrySample, *, now_ms: int | None = None) -> EnqueueResult:
        """持久化一条样本。

        同一 ``event_id`` 重复入队是幂等的（``UNIQUE`` 冲突即跳过）——采集侧重试
        与 MQTT QoS1 的重复投递都会走到这里，不能变成重复补传。
        """
        payload = sample.model_dump_json()
        ts_ms = to_millis(sample.ts)
        enqueued_ms = now_ms if now_ms is not None else int(time.time() * 1000)

        with self._lock:
            stats = self._stats_locked()
            if stats.bytes_used >= self._quota_bytes:
                return EnqueueResult(
                    accepted=False,
                    rows=stats.rows,
                    usage_ratio=stats.usage_ratio,
                    overflow=True,
                )
            inserted = False
            try:
                self._db.execute(
                    "INSERT INTO replay_queue (event_id, ts_ms, enqueued_ms, payload)"
                    " VALUES (?, ?, ?, ?)",
                    (sample.event_id, ts_ms, enqueued_ms, payload),
                )
                self._db.commit()
                inserted = True
            except sqlite3.IntegrityError:
                # 重复 event_id：幂等跳过，不计入 overflow，也不虚报行数
                pass
            return EnqueueResult(
                accepted=True,
                rows=stats.rows + (1 if inserted else 0),
                usage_ratio=stats.usage_ratio,
                overflow=False,
            )

    # ------------------------------------------------------------------ 读取与确认

    def peek(self, limit: int) -> list[tuple[int, TelemetrySample]]:
        """按事件时间取最早的一批，**不删除**。返回 ``(seq, sample)``。"""
        with self._lock:
            cursor = self._db.execute(
                "SELECT seq, payload FROM replay_queue ORDER BY ts_ms ASC, seq ASC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
        return [(int(seq), TelemetrySample.model_validate_json(payload)) for seq, payload in rows]

    def ack(self, seqs: list[int]) -> int:
        """确认已成功投递的条目并删除。返回删除条数。"""
        if not seqs:
            return 0
        placeholders = ",".join("?" * len(seqs))
        with self._lock:
            cursor = self._db.execute(
                f"DELETE FROM replay_queue WHERE seq IN ({placeholders})",  # noqa: S608 - 占位符由长度生成，值仍走参数绑定
                tuple(seqs),
            )
            self._db.commit()
            return cursor.rowcount

    def purge_older_than_ms(self, cutoff_ms: int) -> int:
        """删除早于某时刻的条目。仅用于补偿：保留期外的数据已无补传意义。"""
        with self._lock:
            cursor = self._db.execute("DELETE FROM replay_queue WHERE ts_ms < ?", (cutoff_ms,))
            self._db.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------ 观测

    def stats(self) -> BufferStats:
        with self._lock:
            return self._stats_locked()

    def should_warn(self) -> bool:
        """是否到了新的水位阶梯（调用方据此决定要不要打日志，避免刷屏）。"""
        ratio = self.stats().usage_ratio
        if ratio < self._high_watermark:
            self._last_warned_ratio = 0.0
            return False
        bucket = int(ratio / self._warn_ratio_step)
        if bucket > int(self._last_warned_ratio / self._warn_ratio_step):
            self._last_warned_ratio = ratio
            return True
        return False

    def _stats_locked(self) -> BufferStats:
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(MIN(ts_ms), 0), COALESCE(MAX(ts_ms), 0)"
            " FROM replay_queue"
        ).fetchone()
        rows = int(row[0])
        oldest = int(row[1]) or None
        newest = int(row[2]) or None
        return BufferStats(
            rows=rows,
            bytes_used=self._disk_usage_bytes(),
            quota_bytes=self._quota_bytes,
            oldest_ts_ms=oldest,
            newest_ts_ms=newest,
        )

    def _disk_usage_bytes(self) -> int:
        """库文件 + 回滚日志的实际占用。配额按磁盘占用算，不按行数算。"""
        total = 0
        for suffix in ("", "-journal", "-wal"):
            candidate = Path(f"{self._path}{suffix}")
            if candidate.exists():
                total += candidate.stat().st_size
        return total
