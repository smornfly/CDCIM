"""统一错误体与错误码（RFC 7807 风格，见《接口文档》§1.2 / §5）。

规则（照抄接口文档，不另发明）：

* 响应体只含 ``code`` / ``message`` / ``trace_id`` / ``details``；**不含堆栈、SQL、
  内网路径**（SEC-C-07）。
* ``message`` 是给人看的短句，可改文案；``code`` 是机读契约，**只增不改语义**。
* HTTP 状态码与错误码分离：同一个 401 有 4 个码，客户端靠前缀兜底
  （``AUTH-*`` 一律跳登录），不靠逐个枚举。

本模块只登记 P4 骨架实际会返回的码；其余 20 条在 P5 随接口一起实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse

__all__ = [
    "ApiError",
    "ERROR_STATUS",
    "P4_NOT_IMPLEMENTED",
    "PROBLEM_CONTENT_TYPE",
    "problem_response",
]

PROBLEM_CONTENT_TYPE = "application/problem+json"

#: 错误码 → HTTP 状态码。表外错误码一律 500，并在日志里带 ``unknown_error_code``。
ERROR_STATUS: dict[str, int] = {
    "CDCIM-REQ-001": 400,   # 参数缺失或格式错误
    "CDCIM-REQ-002": 400,   # 枚举越界
    "CDCIM-REQ-003": 400,   # 时间区间非法
    "CDCIM-REQ-006": 413,   # 规模超限
    "CDCIM-AUTH-001": 401,  # 无凭据 / scheme 非 Bearer
    "CDCIM-AUTH-002": 401,  # 令牌无效 / 会话不存在
    "CDCIM-AUTH-003": 401,  # 会话超时
    "CDCIM-AUTH-004": 401,  # 账号锁定或禁用
    "CDCIM-AUTH-005": 403,  # 权限不足
    "CDCIM-AUTH-006": 403,  # 职责分离冲突
    "CDCIM-AUTH-007": 403,  # AI 建议作控制入参
    "CDCIM-RES-001": 404,   # 资源不存在或已归档
    "CDCIM-RES-002": 409,   # 状态机非法迁移
    "CDCIM-SYS-001": 500,   # 服务内部错误
    "CDCIM-SYS-002": 503,   # 依赖不可用
}

#: **P4 骨架专用**：接口在 P4 只登记路由、未实现业务，返回 501。
#: 该码不在《接口文档》§5 的 26 条内，已在《CDCIM-P4-DD-01》§5 差异清单登记；
#: P5 实现对应接口后此码整体消失（不留兼容分支）。
P4_NOT_IMPLEMENTED = "CDCIM-P4-501"


@dataclass
class ApiError(Exception):
    """业务异常的基类。领域层与接口层都抛它，由全局 handler 统一转 JSON。"""

    code: str
    message: str
    details: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    @property
    def http_status(self) -> int:
        if self.code == P4_NOT_IMPLEMENTED:
            return 501
        return ERROR_STATUS.get(self.code, 500)


def problem_response(error: ApiError, trace_id: str) -> JSONResponse:
    """渲染 RFC 7807 风格错误体。``details`` 为空时也要出现（前端按键判断）。"""
    body = {
        "code": error.code,
        "message": error.message,
        "trace_id": trace_id,
        "details": error.details,
    }
    return JSONResponse(status_code=error.http_status, content=body, media_type=PROBLEM_CONTENT_TYPE)
