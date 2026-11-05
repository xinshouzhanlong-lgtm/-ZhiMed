"""管理端账号密码入口（argon2id）。

只给导医台与管理员。初始口令由启动时设置（见 main.py 的 bootstrap），
**不写进任何配置文件**。
"""
from __future__ import annotations

import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .. import state
from ..schemas import Principal
from ..settings import settings

_hasher = PasswordHasher()


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify(username: str, password: str) -> Principal:
    """失败 5 次锁 5 分钟。失败抛 ValueError(code)。"""
    lock_minutes = int(settings.limits.get("admin_lock_minutes", 5))
    max_fail = int(settings.limits.get("admin_max_fail", 5))
    if state.login_fail_count(username, lock_minutes * 60 * 1000) >= max_fail:
        state.audit("lock", username, "password")
        raise ValueError("E_LOCKED")

    staff = state.staff_by_id(username)
    if not staff or not staff.get("password_hash"):
        state.audit("login_fail", username, "password")
        raise ValueError("E_BAD_CREDENTIALS")

    try:
        _hasher.verify(staff["password_hash"], password)
    except VerifyMismatchError:
        state.audit("login_fail", username, "password")
        raise ValueError("E_BAD_CREDENTIALS") from None
    except Exception:  # noqa: BLE001
        state.audit("login_fail", username, "password")
        raise ValueError("E_BAD_CREDENTIALS") from None

    state.audit("login_ok", username, "password")
    now = int(time.time())
    return Principal(
        sub=username,
        role="admin",
        dept_scope=["*"],
        display_name=staff["display_name"],
        entry="password",
        issued_at=now * 1000,
        exp=(now + settings.jwt_ttl_minutes * 60) * 1000,
    )
