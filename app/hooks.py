"""L 层：生命周期钩子 + 确认令牌签发/校验 + 授权判定。

纪律（对应设计文档 §4.5）：
- 钩子在 registry.invoke 与 loop.handle_turn 内**硬编码调用**，不是可选中间件；
- 令牌由本模块签发（L 层），md 侧只消费；**S 层不能给自己发许可**；
- 认证说"你是谁"（deps），授权说"你能做这件事"（本模块），两者不混。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any, Callable, Literal

from . import state
from .schemas import HookResult
from .settings import settings

TOKEN_TTL_MS = 300_000

Channel = Literal["patient_voice", "admin_web", "doctor_web"]

# 通道 → 允许披露/执行的工具白名单（"注册 ≠ 披露 ≠ 授权" 的披露环节）
DISCLOSURE: dict[str, frozenset[str]] = {
    "patient_voice": frozenset({
        "list_on_duty", "get_department", "search_departments",
        "get_doctor", "get_doctor_schedule", "get_doctor_photo",
    }),
    "admin_web": frozenset({
        "list_on_duty", "get_department", "search_departments", "get_doctor",
        "get_doctor_schedule", "get_doctor_photo", "propose_md_patch", "commit_md_patch",
    }),
    "doctor_web": frozenset({"get_doctor", "get_doctor_photo"}),
}


def disclose(channel: str) -> frozenset[str]:
    return DISCLOSURE.get(channel, frozenset())


# ─────────────────────────── 确认令牌 ───────────────────────────


def _mac(raw: str) -> str:
    digest = hmac.new(settings.jwt_secret.encode(), raw.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_token(patch_id: str, after_hash: str, created_at_ms: int) -> str:
    """签发确认令牌：绑定 patch_id + after_hash + 过期时间。"""
    exp = int(created_at_ms) + TOKEN_TTL_MS
    return _mac(f"{patch_id}|{after_hash}|{exp}")


def verify_token(token: str, patch_id: str, after_hash: str) -> bool:
    """校验令牌。exp 从 md_patch 的 created_at 反推，保证令牌本身无状态、可离线验。"""
    if not token:
        return False
    row = state.one("SELECT created_at FROM md_patch WHERE patch_id=?", (patch_id,))
    if not row:
        return False
    exp = int(row["created_at"]) + TOKEN_TTL_MS
    if int(time.time() * 1000) > exp:
        return False
    return hmac.compare_digest(token, _mac(f"{patch_id}|{after_hash}|{exp}"))


# ─────────────────────────── 钩子链 ───────────────────────────


class HookChain:
    def __init__(self) -> None:
        self._buckets: dict[str, list[Callable[[dict[str, Any]], HookResult]]] = {
            "pre_invocation": [],
            "pre_tool_use": [],
            "post_tool_use": [],
            "post_invocation": [],
        }

    def on(self, name: str, fn: Callable[[dict[str, Any]], HookResult]) -> None:
        if name not in self._buckets:
            raise KeyError(f"未知钩子: {name}")
        self._buckets[name].append(fn)

    def run(self, name: str, payload: dict[str, Any]) -> HookResult:
        """任一钩子返回 allow=False 即短路。"""
        for fn in self._buckets[name]:
            res = fn(payload)
            if not res.allow:
                return res
            if res.mutation:
                payload.update(res.mutation)
        return HookResult(allow=True)


hooks = HookChain()


# ─────────────────────────── 限流（单机内存态） ───────────────────────────

_rate: dict[str, list[int]] = {}


def check_rate(device_id: str) -> HookResult:
    limit = int(settings.limits.get("per_device_per_min", 20))
    now = int(time.time() * 1000)
    window = [t for t in _rate.get(device_id, []) if now - t < 60_000]
    if len(window) >= limit:
        _rate[device_id] = window
        return HookResult(allow=False, reason="RATE_LIMITED")
    window.append(now)
    _rate[device_id] = window
    return HookResult(allow=True)


def tool_authorized(channel: str, tool: str) -> HookResult:
    """未披露的工具直接拒绝（患者通道碰不到写工具）。"""
    if tool not in disclose(channel):
        return HookResult(allow=False, reason="NOT_DISCLOSED")
    return HookResult(allow=True)


hooks.on("pre_invocation", lambda p: check_rate(str(p.get("device_id", "unknown"))))
hooks.on("pre_tool_use", lambda p: tool_authorized(str(p.get("channel", "")), str(p.get("tool", ""))))
