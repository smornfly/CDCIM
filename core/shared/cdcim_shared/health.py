"""极小 HTTP 存活端点（仅 P4 骨架，不依赖 FastAPI）。

采集与计算进程是长驻 asyncio 循环，容器编排却需要 HTTP healthcheck。为这两个
进程各起一个 FastAPI 会把容器镜像和内存预算都拖大，所以用标准库起一个只回答
``/healthz`` 的 200 响应。它**不探测任何依赖**——依赖故障由各自的消费循环计数
并在日志里体现，探针失败只代表进程死了。

路径白名单防止把任意请求当作健康成功；响应固定 ``application/json``。
"""

from __future__ import annotations

import asyncio
from typing import Final

__all__ = ["serve_with_health", "start_health_server"]

_BODY: Final[bytes] = b'{"status":"ok","phase":"P4"}'
_NOT_FOUND: Final[bytes] = b'{"status":"not_found"}'
_MAX_HEADER_LINES: Final[int] = 32
_REQUEST_TIMEOUT_S: Final[float] = 5.0


async def start_health_server(port: int, host: str = "0.0.0.0") -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # 整次请求加总超时：只给首行加 2 s，慢客户端仍可用大量 header 行占住任务。
        try:
            await asyncio.wait_for(_respond(reader, writer), timeout=_REQUEST_TIMEOUT_S)
        except (asyncio.TimeoutError, OSError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host=host, port=port)


async def _respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    head = await reader.readline()
    parts = head.decode("ascii", errors="ignore").split(" ")
    path = parts[1] if len(parts) > 1 else ""

    # 有上限的 header 消费：正常探针只有 3~5 行，32 行足够且不会被慢客户端拖死。
    for _ in range(_MAX_HEADER_LINES):
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break

    ok = path.split("?")[0] in ("/healthz", "/readyz")
    body = _BODY if ok else _NOT_FOUND
    status = "200 OK" if ok else "404 Not Found"
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    )
    await writer.drain()


async def serve_with_health(work, port: int | None) -> int:  # noqa: ANN001
    """把健康服务器与业务协程放进同一个事件循环；业务结束即关闭监听。"""
    if port is None:
        return int(await work)
    server = await start_health_server(port)
    try:
        return int(await work)
    finally:
        server.close()
        await server.wait_closed()
