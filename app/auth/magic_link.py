"""签名直链（医生自助入口）。

设计要点（对应设计文档 §7.5）：
- token 放 URL **fragment**（`#t=`），不进 access log / Referer；
- HMAC-SHA256 绑定 doctor_id + jti + exp；
- 72h 有效；**不是一次性，但"上传成功后即失效"**（consumed_at）；
- 可撤销（revoked_at），离职/误发能立刻止血。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from .. import state
from ..schemas import MagicLinkClaims, MagicLinkToken, Principal
from ..settings import settings


def _mac(doctor_id: str, jti: str, exp: int) -> str:
    raw = f"{doctor_id}|{jti}|{exp}"
    digest = hmac.new(settings.magic_link_secret.encode(), raw.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue(doctor_id: str, issued_by: str, ttl_hours: int | None = None) -> MagicLinkToken:
    ttl = ttl_hours if ttl_hours is not None else settings.magic_link_ttl_hours
    jti = secrets.token_urlsafe(32)
    exp = int(time.time()) + ttl * 3600
    token = _mac(doctor_id, jti, exp)
    state.create_magic_link(jti, doctor_id, issued_by, exp)
    state.audit("link_issue", doctor_id, "magic_link")
    url = f"{settings.public_base_url.rstrip('/')}/doctor/#t={token}"
    return MagicLinkToken(token=token, claims=MagicLinkClaims(doctor_id=doctor_id, jti=jti, exp=exp), url=url)


def verify(token: str) -> Principal:
    """校验签名 + 未过期 + 未撤销 + 未消费。失败抛 ValueError(code)。"""
    now = int(time.time())
    rows = state.query(
        "SELECT * FROM magic_link WHERE revoked_at=0 AND consumed_at=0 AND expires_at > ?"
        " ORDER BY created_at DESC LIMIT 200",
        (now,),
    )
    for row in rows:
        expected = _mac(row["doctor_id"], row["jti"], int(row["expires_at"]))
        if hmac.compare_digest(expected, token):
            staff = state.staff_by_id(row["doctor_id"])
            if not staff or not staff["active"]:
                state.audit("link_use_fail", row["doctor_id"], "magic_link")
                raise ValueError("E_STAFF_NOT_FOUND")
            return Principal(
                sub=row["doctor_id"],
                role="doctor",
                dept_scope=[],
                display_name=staff["display_name"] if staff else None,
                entry="magic_link",
                issued_at=now * 1000,
                exp=(now + settings.jwt_ttl_minutes * 60) * 1000,
            )
    state.audit("link_use_fail", "", "magic_link")
    raise ValueError("E_LINK_INVALID")


def consume(token: str) -> bool:
    """上传成功后调用：把对应 jti 标记为已消费，防止链接被转发代传。"""
    now = int(time.time())
    rows = state.query(
        "SELECT * FROM magic_link WHERE revoked_at=0 AND consumed_at=0 AND expires_at > ?",
        (now,),
    )
    for row in rows:
        if hmac.compare_digest(_mac(row["doctor_id"], row["jti"], int(row["expires_at"])), token):
            state.consume_magic_link(row["jti"])
            state.audit("link_use", row["doctor_id"], "magic_link")
            return True
    return False


def revoke(doctor_id: str) -> int:
    n = state.revoke_magic_links(doctor_id)
    state.audit("link_revoke", doctor_id, "magic_link")
    return n
