"""日志配置回归：PrintLoggerFactory + add_logger_name 会在启动时崩溃。"""
from __future__ import annotations

import structlog

from cdcim_shared.logging import configure_logging, get_logger


def test_logger_factory_matches_add_logger_name() -> None:
    # 处理器链含 structlog.stdlib.add_logger_name，它读取 logger.name；
    # PrintLogger 没有该属性，曾导致 api 容器启动即崩溃。
    configure_logging("unit-test", "INFO", json_output=True)
    assert isinstance(
        structlog.get_config()["logger_factory"], structlog.stdlib.LoggerFactory
    )
    get_logger("probe").info("ready", rows=3)  # 不抛异常即可
