"""Principal 签发与校验 —— 全系统唯一签发点。"""
from __future__ import annotations

import time

import jwt

from ..schemas import Principal
from ..settings import settings

ALG = "HS256"


def encode_principal(p: Principal) -> str:
    now = int(time.time())
    payload = {
        "sub": p.sub,
        "role": p.role,
        "dept_scope": p.dept_scope,
        "display_name": p.display_name or "",
        "entry": p.entry,
        "iat": now,
        "exp": now + settings.jwt_ttl_minutes * 60,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALG)


def decode_principal(token: str) -> Principal:
    """验签失败/过期都抛 jwt 异常，由 deps 统一转 401。"""
    data = jwt.decode(token, settings.jwt_secret, algorithms=[ALG])
    return Principal(
        sub=data["sub"],
        role=data["role"],
        dept_scope=list(data.get("dept_scope") or []),
        display_name=data.get("display_name") or None,
        entry=data.get("entry", "password"),
        issued_at=int(data.get("iat", 0)) * 1000,
        exp=int(data.get("exp", 0)) * 1000,
    )
