"""管理员密码与 JWT；停用历史医生身份。"""
from __future__ import annotations

import time

import jwt
import pytest

from app import state
from app.auth import password
from app.auth.principal import decode_principal, encode_principal
from app.schemas import Principal
from app.settings import settings


def test_password_login_ok_and_wrong():
    p = password.verify("admin", "admin123")
    assert p.role == "admin"
    assert p.entry == "password"
    with pytest.raises(ValueError, match="E_BAD_CREDENTIALS"):
        password.verify("admin", "wrong-password")


def test_non_admin_staff_cannot_log_in():
    state.upsert_staff("legacy-doctor", "doctor", "legacy-doctor", [], "历史医生",
                       password.hash_password("old-password"))
    with pytest.raises(ValueError, match="E_BAD_CREDENTIALS"):
        password.verify("legacy-doctor", "old-password")


def test_failed_logins_lock_account():
    state.seed_admin("lock-test-admin", password.hash_password("password"))
    for _ in range(int(settings.limits.get("admin_max_fail", 5))):
        with pytest.raises(ValueError, match="E_BAD_CREDENTIALS"):
            password.verify("lock-test-admin", "wrong")
    with pytest.raises(ValueError, match="E_LOCKED"):
        password.verify("lock-test-admin", "password")


def test_jwt_round_trip():
    p = Principal(sub="admin", role="admin", dept_scope=["*"])
    decoded = decode_principal(encode_principal(p))
    assert decoded.sub == p.sub
    assert decoded.role == "admin"
    assert decoded.entry == "password"
    assert decoded.exp > decoded.issued_at


def test_jwt_with_wrong_signature_rejected():
    token = jwt.encode({"sub": "admin", "role": "admin", "exp": int(time.time()) + 60},
                       "wrong-signing-secret", algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        decode_principal(token)


def test_expired_jwt_rejected():
    token = jwt.encode({"sub": "admin", "role": "admin", "exp": int(time.time()) - 60},
                       settings.jwt_secret, algorithm="HS256")
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_principal(token)


@pytest.mark.parametrize("role,entry", [("doctor", "magic_link"), ("admin", "magic_link")])
def test_retired_identity_rejected(role, entry):
    token = jwt.encode({"sub": "legacy", "role": role, "entry": entry,
                        "exp": int(time.time()) + 60}, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(ValueError):
        decode_principal(token)
