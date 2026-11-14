"""认证域：管理端密码登录与 JWT 身份。

OA 单点只留契约（provider.py），本期 `NotConfiguredProvider` 返回 501。
"""

from .principal import decode_principal, encode_principal  # noqa: F401
