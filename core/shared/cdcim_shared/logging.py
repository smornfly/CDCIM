"""结构化日志：JSON 单行输出，字段含 ``trace_id`` / ``service`` / ``event``。

为什么不用 ``print`` / 默认 logging（编码规范 PY-08、B-01）：

* 容器里日志靠字段检索，不靠正则抓文本；``trace_id`` 必须是一等字段，
  才能和 ``audit_log``、告警推送串成一条链路（NFR-TEST-04）。
* 位置参数即 **``event``（稳定短名）**：``log.info("telemetry_buffered", rows=3)``
  产出 ``{"event": "telemetry_buffered", ...}``。告警规则与 Grafana 面板只依赖
  ``event``，改文案不会打破看板；给人看的句子放 ``msg`` 字段。

用法::

    configure_logging(settings.service_name, settings.log_level)
    log = get_logger(__name__)
    with trace_scope("4f1c9b2e8a7d4c31"):
        log.info("batch_written", msg="500 rows flushed", rows=500)
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

import structlog

__all__ = [
    "bind_trace",
    "configure_logging",
    "get_logger",
    "new_trace_id",
    "trace_scope",
]

_CONFIGURED: bool = False
_SERVICE: Final[str] = "cdcim"


def configure_logging(service: str, level: str = "INFO", *, json_output: bool = True) -> None:
    """进程启动时调用一次。重复调用只更新级别，不重复挂 handler（避免日志翻倍）。"""
    global _CONFIGURED, _SERVICE
    _SERVICE = service

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_service_and_event,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.JSONRenderer(ensure_ascii=False) if json_output else
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        # 必须用 stdlib LoggerFactory：处理器链里的 add_logger_name 读取
        # logger.name，PrintLogger 没有该属性（曾在 api 启动时抛 AttributeError）。
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if not _CONFIGURED:
        # 把标准库（uvicorn / paho / aiokafka）的日志也纳入同一输出格式，
        # 否则一半 JSON 一半文本，采集侧解析规则要写两套。
        logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
        for noisy in ("aiokafka", "paho", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _CONFIGURED = True


def _add_service_and_event(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """补齐 ``service``；``event`` 由 structlog 的位置参数提供，缺失时补 ``-``。"""
    event_dict.setdefault("service", _SERVICE)
    event_dict.setdefault("event", "-")
    return event_dict


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """取 logger。name 建议传 ``__name__``，便于按模块过滤。"""
    return structlog.get_logger(name or _SERVICE)


def new_trace_id() -> str:
    """生成 16 位十六进制 trace_id，长度与契约 ``maxLength: 32`` 及审计表一致。"""
    import secrets

    return secrets.token_hex(8)


def bind_trace(trace_id: str) -> None:
    """把 trace_id 绑到当前协程上下文；后续同协程日志自动携带。"""
    structlog.contextvars.bind_contextvars(trace_id=trace_id)


@contextmanager
def trace_scope(trace_id: str | None = None) -> Iterator[str]:
    """作用域内绑定 trace_id，退出时解绑（协程复用不会串味）。"""
    resolved = trace_id or new_trace_id()
    structlog.contextvars.bind_contextvars(trace_id=resolved)
    try:
        yield resolved
    finally:
        structlog.contextvars.unbind_contextvars("trace_id")
