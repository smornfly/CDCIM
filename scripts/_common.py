"""验证脚本共用工具：参数、HTTP 调用、表格输出。

仅依赖 Python 标准库（`urllib.request`），无需 pip 安装 requests；
因此在最小 Ubuntu 环境里也能直接跑。脚本内出现 `os.getenv` 仅用于
读取验证脚本自身的运行参数（`CDCIM_BASE_URL`），不涉及业务配置。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL_ENV = "CDCIM_BASE_URL"
MQTT_HOST_ENV = "CDCIM_MQTT_HOST"
MQTT_PORT_ENV = "CDCIM_MQTT_PORT"
DEFAULT_TIMEOUT_S = 5.0


# ------------------------------------------------------------------ 参数
def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=os.getenv(BASE_URL_ENV, ""),
        help=f"系统访问地址，如 http://127.0.0.1:8080；默认取环境变量 {BASE_URL_ENV}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"单次 HTTP 超时（秒），默认 {DEFAULT_TIMEOUT_S}",
    )


def normalize_base_url(raw: str) -> str:
    return raw.strip().rstrip("/")


def require_base_url(base_url: str) -> str:
    """不允许在脚本里硬编码 localhost，缺失时必须显式给出。"""
    if not base_url:
        die(
            "未指定系统地址。",
            f"请加 --base-url http://<host>:8080，或先 export {BASE_URL_ENV}=http://<host>:8080",
        )
    if not base_url.startswith(("http://", "https://")):
        die(f"--base-url 必须以 http:// 或 https:// 开头，当前为 {base_url!r}")
    return normalize_base_url(base_url)


def die(message: str, hint: str | None = None, code: int = 2) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    if hint:
        print(f"[HINT] {hint}", file=sys.stderr)
    raise SystemExit(code)


def warn(message: str, hint: str | None = None) -> None:
    print(f"[WARN] {message}", file=sys.stderr)
    if hint:
        print(f"[HINT] {hint}", file=sys.stderr)


def info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)


# ------------------------------------------------------------------ HTTP
@dataclass
class HttpResult:
    status: int
    body: Any
    elapsed_ms: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and 200 <= self.status < 300


def http_request(
    base_url: str,
    path: str,
    timeout: float,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HttpResult:
    url = f"{base_url}{path}"
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = dict(headers or {})
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    request = Request(url, data=data, headers=headers, method=method)

    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - 地址由 --base-url 显式给出
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return HttpResult(0, None, elapsed, f"{type(exc).__name__}: {exc}")
    elapsed = (time.perf_counter() - started) * 1000
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw[:500]
    return HttpResult(status, body, elapsed)


def problem_hint(result: HttpResult) -> str:
    """把接口文档 §5 的错误码翻译成可操作建议。"""
    code = ""
    if isinstance(result.body, dict):
        code = str(result.body.get("code", ""))
    if result.error:
        return ("连不上服务：确认容器已起（scripts/check_stack.py）、端口与 --base-url 一致、"
                "反向代理未拦截")
    if code.startswith("CDCIM-AUTH") or result.status in (401, 403):
        return "认证/授权失败：先用有效会话令牌登录，确认账号具备对应角色"
    if code.startswith("CDCIM-REQ"):
        return "参数被拒绝：核对 point_id/device_id 是否存在于台账，时间区间是否合法"
    if code.startswith("CDCIM-RES") or result.status == 404:
        return "资源不存在：确认测点已在台账导入（POST /points/import），或换一个已存在的 point_id"
    if code.startswith("CDCIM-SYS") or result.status >= 500:
        trace = ""
        if isinstance(result.body, dict):
            trace = str(result.body.get("trace_id", ""))
        return f"服务端异常：凭 trace_id={trace or 'N/A'} 查 api-service 日志与依赖健康状态"
    return "检查接口路径与方法是否与 docs/04-详细设计/07-接口文档.md 一致"


def get_json(
    base_url: str,
    path: str,
    timeout: float,
    params: dict[str, Any] | None = None,
) -> HttpResult:
    return http_request(base_url, path, timeout, params=params)


# ------------------------------------------------------------------ 时间
def parse_iso_ms(text: str) -> int | None:
    """ISO 8601 → epoch 毫秒；解析失败返回 None。"""
    if not isinstance(text, str) or not text:
        return None
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso_from_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone().isoformat(
        timespec="milliseconds"
    )


# ------------------------------------------------------------------ 输出
def shorten(text: str, limit: int = 60) -> str:
    """按显示宽度截断，避免表格被超长异常信息撑爆。"""
    if _display_width(text) <= limit:
        return text
    out = ""
    width = 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if width + step > limit - 1:
            break
        out += ch
        width += step
    return out + "…"


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [_display_width(str(h)) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _display_width(str(cell)))
    line = "+".join("-" * (width + 2) for width in widths)
    print("+" + line + "+")
    print("| " + " | ".join(_pad(str(h), widths[i]) for i, h in enumerate(headers)) + " |")
    print("+" + line + "+")
    for row in rows:
        print("| " + " | ".join(_pad(str(c), widths[i]) for i, c in enumerate(row)) + " |")
    print("+" + line + "+")


def dump_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    info(f"结果已写入 {path}")
