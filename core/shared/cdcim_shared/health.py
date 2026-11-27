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

__all__ = ["start_health_server"]

_BODY: Final[bytes] = b'{"status":"ok","phase":"P4"}'
_NOT_FOUND: Final[bytes] = b'{"status":"not_found"}'


async def start_health_server(port: int, host: str = "0.0.0.0") -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readline(), timeout=2.0)
            path = head.decode("ascii", errors="ignore").split(" ")[1] if len(head.split(" ")) > 1 else ""
            while await reader.readline() not in (b"\r\n", b"\n", b""):
                pass
            ok = path.split("?")[0] in ("/healthz", "/readyz")
            body = _BODY if ok else _NOT_FOUND
            status = "200 OK" if ok else "404 Not Found"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (asyncio.TimeoutError, IndexError, OSError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host=host, port=port)


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
