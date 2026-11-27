"""查询服务入口（P4 骨架）。

职责边界：本文件**只做 HTTP 适配**——鉴权、参数校验、调用只读查询、统一错误体。
领域规则（告警状态机、抑制、重算）不在这里；P5 再接入 alarm-engine 的客户端。

启动自检（``--preflight``）在**监听端口之前**完成，失败即非零退出；容器编排依赖
它作为 healthcheck 前的一道闸门。设计意图：配置错误必须在启动期暴露，不能在
第一次查询时才以 500 的形式出现（编码规范 PY-09）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 允许以脚本方式运行（容器里没有把 core/api 装成包）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

from fastapi import Depends, FastAPI, Query, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from cdcim_shared.config import Settings, get_settings  # noqa: E402
from cdcim_shared.logging import get_logger  # noqa: E402
from cdcim_shared.timeutil import utc_now  # noqa: E402

from errors import ApiError, P4_NOT_IMPLEMENTED, problem_response  # noqa: E402
from security import (  # noqa: E402
    CurrentUser,
    assert_auth_mode_supported,
    issue_placeholder_token,
    require_permission,
    verify_login,
)

__all__ = ["app", "main"]

log = get_logger("api")


# ------------------------------------------------------------------ 请求模型


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 28_800
    role: str
    warning: str = "P4 占位令牌，无签名、可伪造；P5 替换为服务端会话令牌"


class PointValue(BaseModel):
    point_id: int
    ts: str
    value: float | int | bool | None
    quality: str


class HistoryPoint(BaseModel):
    ts: str
    value: float | int | bool | None


class HistoryResponse(BaseModel):
    point_id: int
    interval: str
    points: list[HistoryPoint]
    truncated: bool = False


# ------------------------------------------------------------------ 生命周期


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001, ARG001
    settings = get_settings()
    assert_auth_mode_supported(settings)  # 不支持的模式必须在监听前失败
    log.info("api_started", config=settings.safe_summary())
    yield
    log.info("api_stopped")


app = FastAPI(
    title="CDCIM 查询服务（P4 骨架）",
    version="0.1.0",
    description="仅实现健康、登录、最新值与历史查询；其余接口返回 501，P5 补齐。",
    lifespan=lifespan,
)


# ------------------------------------------------------------------ 横切


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):  # noqa: ANN001
    incoming = request.headers.get("x-trace-id", "").strip()[:32]
    trace_id = incoming or utc_now().strftime("%Y%m%d%H%M%S") + _short_id()
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["x-trace-id"] = trace_id
    return response


def _short_id() -> str:
    import secrets

    return secrets.token_hex(4)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:  # noqa: ARG001
    trace_id = getattr(request.state, "trace_id", "unknown")
    log.warn("api_error", code=exc.code, message=exc.message, trace_id=trace_id)
    return problem_response(exc, trace_id)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ARG001
    details = [
        {
            "field": ".".join(str(p) for p in err.get("loc", [])[1:]),
            "reason": err.get("msg", "invalid"),
        }
        for err in exc.errors()
    ]
    return problem_response(ApiError("CDCIM-REQ-001", "请求参数不正确", details), "validation")


# ------------------------------------------------------------------ 健康检查


@app.get("/healthz", include_in_schema=True, summary="存活探针（不依赖任何中间件）")
async def healthz() -> dict[str, str]:
    """**只回答进程活着**。不查 PG/TDengine/Redis——依赖故障应由 readyz 表达。"""
    return {"status": "ok", "phase": "P4", "at": utc_now().isoformat()}


@app.get("/readyz", summary="就绪探针（检查只读查询所依赖的存储）")
async def readyz() -> dict[str, object]:
    settings = get_settings()
    checks: dict[str, str] = {}
    try:
        checks["pg"] = await asyncio.wait_for(_check_pg(settings), timeout=settings.pg_command_timeout_s)
    except Exception:  # noqa: BLE001 - 探针必须把任何异常转成结构化结果
        checks["pg"] = "unavailable"
    try:
        checks["tdengine"] = await asyncio.wait_for(
            asyncio.to_thread(_check_tdengine, settings), timeout=settings.tdengine_timeout_s
        )
    except Exception:  # noqa: BLE001
        checks["tdengine"] = "unavailable"
    ready = all(v == "ok" for v in checks.values())
    return {"ready": ready, "checks": checks}


async def _check_pg(settings: Settings) -> str:
    import asyncpg

    conn = await asyncpg.connect(dsn=settings.pg_dsn(), timeout=settings.pg_command_timeout_s)
    try:
        value = await conn.fetchval("SELECT 1")
        return "ok" if value == 1 else "degraded"
    finally:
        await conn.close()


def _check_tdengine(settings: Settings) -> str:
    import taosrest

    conn = taosrest.connect(
        url=f"http://{settings.tdengine_host}:{settings.tdengine_rest_port}",
        user=settings.tdengine_user,
        password=settings.tdengine_password.get_secret_value(),
        database=settings.tdengine_database,
        timeout=settings.tdengine_timeout_s,
    )
    try:
        rows = conn.query("SELECT 1")
        return "ok" if rows else "degraded"
    finally:
        conn.close()


# ------------------------------------------------------------------ 认证


@app.post("/api/v1/auth/login", response_model=LoginResponse, summary="P4 占位登录")
async def login(body: LoginRequest) -> LoginResponse:
    """P5-REPLACE：服务端会话令牌 + 失败计数 + 管理员 TOTP。"""
    settings = get_settings()
    user = verify_login(body.username, body.password)
    log.info("login_ok", username=user.username, role=user.role, auth_mode=settings.auth_mode)
    return LoginResponse(
        access_token=issue_placeholder_token(user),
        expires_in=settings.auth_session_ttl_min * 60,
        role=user.role,
    )


# ------------------------------------------------------------------ 查询


@app.get(
    "/api/v1/points/{point_id}/latest",
    response_model=PointValue,
    summary="测点最新值",
)
async def latest(
    point_id: int = Query(ge=1),
    _user: CurrentUser = Depends(require_permission("point:read")),
) -> PointValue:
    """从 TDengine 子表 ``dcim.m_<point_id>`` 取最后一条。子表不存在视为 404。"""
    if point_id <= 0:
        raise ApiError("CDCIM-REQ-001", "point_id 必须为正整数")
    settings = get_settings()
    sql = (
        f"SELECT last(ts, value), last(ts, quality) FROM {settings.tdengine_database}.m_{point_id}"
    )
    rows = await _td_query(settings, sql)
    if not rows or rows[0][0] is None:
        raise ApiError("CDCIM-RES-001", "测点不存在或暂无数据")
    ts, value, _quality_ts, quality = rows[0]
    return PointValue(point_id=point_id, ts=str(ts), value=value, quality=str(quality or "good"))


@app.get(
    "/api/v1/points/{point_id}/history",
    response_model=HistoryResponse,
    summary="测点历史（原始 / 1m / 5m / 1h）",
)
async def history(
    point_id: int = Query(ge=1),
    start: str = Query(alias="from", description="ISO 8601，带时区"),
    end: str = Query(alias="to", description="ISO 8601，带时区"),
    interval: str = Query(default="1m", pattern="^(raw|1m|5m|1h)$"),
    _user: CurrentUser = Depends(require_permission("point:read")),
) -> HistoryResponse:
    """区间与粒度的校验顺序固定：先格式 → 再跨度 → 最后查库。

    ``interval=raw`` 且跨度 > 7 天时拒绝：180 天原始表 8 点叠加曾在 P2 被估算为
    8.3 s，超时风险高；降采样命中后同区间约 104 ms（容量文档 v0.2）。
    """
    t0, t1 = _parse_window(start, end)
    span_days = (t1 - t0).total_seconds() / 86400.0
    settings = get_settings()
    if span_days <= 0 or span_days > settings.api_history_max_span_days:
        raise ApiError(
            "CDCIM-REQ-003",
            f"查询区间非法：须在 (0, {settings.api_history_max_span_days}] 天内",
        )
    if interval == "raw" and span_days > settings.api_raw_max_span_days:
        raise ApiError(
            "CDCIM-REQ-006",
            f"原始粒度最多查 {settings.api_raw_max_span_days} 天，请用 interval=1m/5m/1h",
        )
    table = f"{settings.tdengine_database}.m_{point_id}" if interval == "raw" else (
        f"{settings.tdengine_downsample_db}_{interval}.m_{point_id}"
    )
    rows = await _td_query(
        settings,
        f"SELECT ts, value FROM {table} WHERE ts >= '{_iso(t0)}' AND ts <= '{_iso(t1)}' ORDER BY ts",
    )
    return HistoryResponse(
        point_id=point_id,
        interval=interval,
        points=[HistoryPoint(ts=str(r[0]), value=r[1]) for r in rows],
        truncated=len(rows) >= 5000,
    )


def _parse_window(start: str, end: str) -> tuple[datetime, datetime]:
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("CDCIM-REQ-003", "时间格式须为带时区的 ISO 8601") from None
    if t0.tzinfo is None or t1.tzinfo is None:
        raise ApiError("CDCIM-REQ-003", "时间必须带时区偏移，不接受 naive datetime")
    return t0, t1


def _iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="milliseconds")


async def _td_query(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    """把阻塞的 taosrest 调用放进线程池，避免堵住事件循环。"""
    if settings.tdengine_dry_run:
        log.info("tdengine_dry_run", sql=sql[:160])
        return []
    try:
        return await asyncio.to_thread(_td_query_sync, settings, sql)
    except Exception as exc:  # noqa: BLE001 - 向上映射为 503，不泄漏驱动错误文本
        log.error("tdengine_query_failed", error_type=type(exc).__name__, trace_id="")
        raise ApiError("CDCIM-SYS-002", "时序存储暂不可用") from None


def _td_query_sync(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    import taosrest

    conn = taosrest.connect(
        url=f"http://{settings.tdengine_host}:{settings.tdengine_rest_port}",
        user=settings.tdengine_user,
        password=settings.tdengine_password.get_secret_value(),
        database=settings.tdengine_database,
        timeout=settings.tdengine_timeout_s,
    )
    try:
        return list(conn.query(sql))
    finally:
        conn.close()


# ------------------------------------------------------------------ 路由占位（P5 实现）


@app.patch("/api/v1/alarms/{alarm_id}/ack", summary="确认告警（P5 实现）")
async def ack_placeholder(
    alarm_id: int,
    _user: CurrentUser = Depends(require_permission("alarm:ack")),
) -> None:
    raise ApiError(P4_NOT_IMPLEMENTED, f"告警 {alarm_id} 的确认接口在 P5 实现")


@app.post("/api/v1/data/recompute", summary="按区间重算降采样（P5 实现）")
async def recompute_placeholder(
    _user: CurrentUser = Depends(require_permission("data:repair")),
) -> None:
    raise ApiError(P4_NOT_IMPLEMENTED, "重算接口在 P5 实现；P4 仅登记路由与权限点")


# ------------------------------------------------------------------ CLI


def preflight() -> None:
    settings = get_settings()
    assert_auth_mode_supported(settings)
    print("preflight OK:", settings.safe_summary())


def main() -> None:
    parser = argparse.ArgumentParser(description="CDCIM 查询服务（P4 骨架）")
    parser.add_argument("--preflight", action="store_true", help="只校验配置后退出")
    parser.add_argument("--once", action="store_true", help="冒烟模式：preflight + 打印路由")
    args = parser.parse_args()
    if args.preflight or args.once:
        preflight()
        if args.once:
            for route in app.routes:
                methods = getattr(route, "methods", None)
                print(f"  {','.join(sorted(methods)) if methods else '-':12s} {route.path}")
        return
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_config=None)


if __name__ == "__main__":
    main()
