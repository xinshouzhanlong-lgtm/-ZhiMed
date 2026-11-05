"""OA 单点：只留契约，本期不实现（对应设计文档 §3.4 / §7.5）。

为什么留契约而不是删掉：
删了以后接 OA 要动 principal.py（加 entry 枚举）、deps.py（加路由）、staff.py（改映射键）
三处并重测；留一个 Protocol + 一个 501 实现，下期只加一个实现文件。成本差约 10 倍。

下期接入步骤：
  1. 新增 `providers/cas.py`（或 oauth2.py / oidc.py）实现 SsoProvider；
  2. 在 `get_provider()` 里按 config/auth.json 的 `sso.provider` 返回该实现；
  3. 在 main.py 加两条路由：GET /auth/oa/start、GET /auth/oa/callback；
  4. `AuthEntry` 增加 "oa_sso"；其余（Principal、下游业务）完全不动。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class SsoIdentity(BaseModel):
    """OA 断言归一化结果。employee_no 是唯一映射键。"""

    employee_no: str
    display_name: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)  # 原始断言，仅审计留痕，不进业务


@runtime_checkable
class SsoProvider(Protocol):
    name: str

    def build_authorize_url(self, state: str, nonce: str) -> str: ...

    async def exchange(self, code: str, state: str) -> SsoIdentity: ...


class NotConfiguredProvider:
    """本期唯一实现：任何调用都返回 501，避免"假装成功"。"""

    name = "none"

    def build_authorize_url(self, state: str, nonce: str) -> str:
        raise NotImplementedError("OA 单点未配置（下期接入）")

    async def exchange(self, code: str, state: str) -> SsoIdentity:
        raise NotImplementedError("OA 单点未配置（下期接入）")


def get_provider(name: str = "none") -> SsoProvider:
    if name == "none":
        return NotConfiguredProvider()
    raise NotImplementedError(f"OA 单点协议 {name} 尚未实现（下期接入）")
