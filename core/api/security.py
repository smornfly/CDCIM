"""鉴权与授权（P4 占位实现，P5 整体替换为服务端会话令牌）。

**这一层的实现不是安全机制，禁止在生产启用。** 说明清楚它是什么、不是什么：

* 令牌是"三段式占位串"（``base64(header).base64(payload).p4-placeholder``），
  **没有签名、没有校验、不可撤销**。任何人改 payload 里的 ``role`` 就能提权。
* 它存在的唯一目的：让 P4 的三个接口能跑通"认证 → 授权 → 业务"的调用链，
  并把 401/403 的判定顺序、错误码、依赖注入的形状先固定下来。
* **P5 替换点**（三处，均已在本文件显式标注 ``P5-REPLACE``）：
  1. 令牌载体：改为《接口文档》§4.1 的服务端会话令牌（opaque token，存 Redis
     ``cdcim:sess:*``），从而支持改密/改角色/禁用后**立即**失效；
  2. 账号来源：改为 ``user_account`` + ``role`` + ``role_permission`` 三表联查，
     ``deny`` 优先于 ``grant``，并落地数据范围（``role.data_scope``）；
  3. 失败计数与锁定：连续 5 次失败锁 10 min，返回 401 ``CDCIM-AUTH-004``。

授权模型：角色 → 权限点（19 功能点，D-39）。**管理员不是超级角色**：它没有
``alarm:ack`` / ``rule:write`` / ``device:write``——《接口文档》§4.3 易错点 1
明确要求这一点，否则"删审计掩盖误确认"的动机成立。
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cdcim_shared.config import Settings, get_settings
from cdcim_shared.timeutil import utc_now

from errors import ApiError

__all__ = [
    "CurrentUser",
    "DEV_ACCOUNTS",
    "ROLE_ADMIN",
    "ROLE_ENGINEER",
    "ROLE_VIEWER",
    "assert_auth_mode_supported",
    "authenticate",
    "issue_placeholder_token",
    "require_permission",
    "verify_login",
]

ROLE_VIEWER: Final[str] = "viewer"
ROLE_ENGINEER: Final[str] = "engineer"
ROLE_ADMIN: Final[str] = "admin"

#: 角色 → 权限点。只列 P4 用到的子集；P5 从 ``role_permission`` 表加载同一形状。
ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    ROLE_VIEWER: frozenset({"point:read", "alarm:read"}),
    ROLE_ENGINEER: frozenset(
        {
            "point:read",
            "alarm:read",
            "alarm:ack",
            "alarm:hold",
            "rule:write",
            "device:write",
            "report:export",
            "linkage:write",
        }
    ),
    ROLE_ADMIN: frozenset({"point:read", "alarm:read", "audit:read", "data:repair"}),
}

#: 演示账号。密码只用于本地冒烟；生产账号来自 ``user_account``（PBKDF2/Argon2 哈希）。
DEV_ACCOUNTS: Final[dict[str, tuple[str, str, int]]] = {
    "dev-viewer": ("dev-pass", ROLE_VIEWER, 101),
    "dev-engineer": ("dev-pass", ROLE_ENGINEER, 102),
    "dev-admin": ("dev-pass", ROLE_ADMIN, 103),
}

_BEARER = HTTPBearer(auto_error=False, description="P4 占位令牌；P5 换服务端会话令牌")


@dataclass(frozen=True)
class CurrentUser:
    """请求上下文里的当前用户。``permissions`` 已展开，避免每个接口再查一次。"""

    user_id: int
    username: str
    role: str
    permissions: frozenset[str]

    def has(self, permission: str) -> bool:
        return permission in self.permissions


def assert_auth_mode_supported(settings: Settings) -> None:
    """启动期自检：明确不支持的模式要**立刻失败**，而不是运行时退化成放行。"""
    if settings.auth_mode != "placeholder":
        raise RuntimeError(
            f"auth_mode={settings.auth_mode!r} is not implemented in P4; "
            "only 'placeholder' is supported until P5 ships the session authenticator"
        )


def issue_placeholder_token(user: CurrentUser) -> str:
    """P5-REPLACE：改为服务端会话令牌（随机 256 bit，存 Redis，可即时撤销）。"""
    header = _b64({"alg": "none", "typ": "P4-PLACEHOLDER"})
    payload = _b64(
        {
            "sub": user.user_id,
            "usr": user.username,
            "role": user.role,
            # 8 h 绝对上限（接口文档 §4.1）；此处仅用于演示过期路径
            "exp": int((utc_now() + timedelta(hours=8)).timestamp()),
        }
    )
    return f"{header}.{payload}.p4-placeholder"


def authenticate(token: str) -> CurrentUser:
    """P5-REPLACE：查 Redis 会话索引 + 校验空闲 30 min / 绝对 8 h + 账号状态。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise ApiError("CDCIM-AUTH-002", "令牌格式无效")
    try:
        payload = json.loads(_unb64(parts[1]))
    except (ValueError, json.JSONDecodeError):
        raise ApiError("CDCIM-AUTH-002", "令牌内容无法解析") from None

    role = str(payload.get("role", ""))
    if role not in ROLE_PERMISSIONS:
        raise ApiError("CDCIM-AUTH-002", "令牌中的角色不存在")
    exp = int(payload.get("exp", 0))
    if exp and exp < int(utc_now().timestamp()):
        raise ApiError("CDCIM-AUTH-003", "会话已超时，请重新登录")

    return CurrentUser(
        user_id=int(payload.get("sub", 0)),
        username=str(payload.get("usr", "")),
        role=role,
        permissions=ROLE_PERMISSIONS[role],
    )


def verify_login(username: str, password: str) -> CurrentUser:
    """校验演示账号。失败提示统一为"账号或凭据错误"，不区分账号是否存在。

    P5-REPLACE：改为 ``user_account`` 查询 + 口令哈希校验 + 失败计数，
    并在此处接入 TOTP 第二步（管理员必须携带 ``otp_code``）。
    """
    record = DEV_ACCOUNTS.get(username)
    if record is None:
        # 无论账号是否存在都走一次比较，避免用响应时间枚举账号
        secrets.compare_digest(password, secrets.token_hex(8))
        raise ApiError("CDCIM-AUTH-002", "账号或凭据错误")
    expected, role, user_id = record
    if not secrets.compare_digest(password, expected):
        raise ApiError("CDCIM-AUTH-002", "账号或凭据错误")
    return CurrentUser(
        user_id=user_id,
        username=username,
        role=role,
        permissions=ROLE_PERMISSIONS[role],
    )


def require_permission(
    permission: str,
) -> Callable[[HTTPAuthorizationCredentials | None], CurrentUser]:
    """依赖注入工厂：``Depends(require_permission("point:read"))``。

    判定顺序与《接口文档》§4.2 一致：**先认证（401），再授权（403）**；
    无权时不探测资源是否存在（不给越权者用 404/403 差异枚举资源的机会）。
    """

    def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> CurrentUser:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise ApiError("CDCIM-AUTH-001", "缺少 Bearer 凭据")
        user = authenticate(credentials.credentials)
        if not user.has(permission):
            raise ApiError(
                "CDCIM-AUTH-005",
                "权限不足",
                details=[{"required_permission": permission, "role": user.role}],
            )
        return user

    return dependency


def _b64(data: dict[str, object]) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(segment: str) -> str:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding).decode()
