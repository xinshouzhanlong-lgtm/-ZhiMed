"""FastAPI 依赖：认证（你是谁）与授权（你能做这件事）分离。

认证只产出 Principal；授权判定在 hooks.pre_tool_use 与 make_photo_grant 里。
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from .schemas import PhotoWriteGrant, Principal
from .auth import principal as principal_mod


def _extract(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")
    return authorization.split(" ", 1)[1].strip()


def get_principal(authorization: str | None = Header(default=None)) -> Principal:
    token = _extract(authorization)
    try:
        return principal_mod.decode_principal(token)
    except Exception:  # noqa: BLE001 — 验签失败/过期统一 401，不泄露细节
        raise HTTPException(status_code=401, detail="令牌无效或已过期") from None


def get_admin(authorization: str | None = Header(default=None)) -> Principal:
    p = get_principal(authorization)
    if p.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return p


def make_photo_grant(p: Principal, doctor_id: str, consent_ack: bool) -> PhotoWriteGrant:
    """双角色上传口子的授权判定。

    admin  → 代传，必须勾选"已取得肖像授权"，否则 422
    doctor → 本人自传，consent_source=doctor_self；越权 403 且不提示目标是否存在
    """
    if p.role == "doctor":
        if p.sub != doctor_id:
            raise HTTPException(status_code=403, detail="只能修改本人的照片")
        return PhotoWriteGrant(subject=p.sub, role="doctor", target_doctor_id=doctor_id,
                               consent_source="doctor_self")
    if p.role == "admin":
        if not consent_ack:
            raise HTTPException(status_code=422, detail="代传必须确认已取得该医生肖像使用授权")
        return PhotoWriteGrant(subject=p.sub, role="admin", target_doctor_id=doctor_id,
                               consent_source="admin_proxy")
    raise HTTPException(status_code=403, detail="无权上传")
