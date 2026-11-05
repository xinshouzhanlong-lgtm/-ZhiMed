"""认证域：两入口（签名直链 / 管理端密码）收敛为一个 Principal。

OA 单点只留契约（provider.py），本期 `NotConfiguredProvider` 返回 501。
"""

from .principal import decode_principal, encode_principal  # noqa: F401
