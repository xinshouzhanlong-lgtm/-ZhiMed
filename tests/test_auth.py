"""认证：直链签名/过期/撤销/消费 + 工号未开通即拒绝（文档 §7.5）。"""
from __future__ import annotations

import pytest

from app import state
from app.auth import magic_link


@pytest.fixture()
def doctor_staff():
    state.upsert_staff("doc-test", "doctor", "doc-test", ["dept-cardio"], "测试医生")
    yield "doc-test"


def test_issue_and_verify(doctor_staff):
    tok = magic_link.issue(doctor_staff, issued_by="admin")
    p = magic_link.verify(tok.token)
    assert p.role == "doctor"
    assert p.sub == doctor_staff
    assert p.entry == "magic_link"


def test_token_url_uses_fragment(doctor_staff):
    tok = magic_link.issue(doctor_staff, issued_by="admin")
    assert "#t=" in tok.url, "token 必须放 fragment，不能进 query/日志"


def test_forged_token_rejected(doctor_staff):
    magic_link.issue(doctor_staff, issued_by="admin")
    with pytest.raises(ValueError) as ei:
        magic_link.verify("x" * 43)
    assert "E_LINK_INVALID" in str(ei.value)


def test_revoked_token_rejected(doctor_staff):
    tok = magic_link.issue(doctor_staff, issued_by="admin")
    magic_link.revoke(doctor_staff)
    with pytest.raises(ValueError):
        magic_link.verify(tok.token)


def test_consumed_token_rejected(doctor_staff):
    tok = magic_link.issue(doctor_staff, issued_by="admin")
    assert magic_link.consume(tok.token) is True
    with pytest.raises(ValueError):
        magic_link.verify(tok.token)


def test_unregistered_staff_rejected():
    """工号未在 staff 表 → 拒绝（绝不自动建号）。"""
    tok = magic_link.issue("doc-nobody", issued_by="admin")
    with pytest.raises(ValueError) as ei:
        magic_link.verify(tok.token)
    assert "E_STAFF_NOT_FOUND" in str(ei.value)


def test_expired_token_rejected(doctor_staff):
    tok = magic_link.issue(doctor_staff, issued_by="admin", ttl_hours=-1)
    with pytest.raises(ValueError):
        magic_link.verify(tok.token)


def test_password_login_ok_and_wrong():
    from app.auth import password

    p = password.verify("admin", "admin123")
    assert p.role == "admin"
    with pytest.raises(ValueError):
        password.verify("admin", "wrong-password")
