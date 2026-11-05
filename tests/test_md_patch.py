"""md 六道门禁（文档 §7.2）。"""
from __future__ import annotations

import pytest

from app.hooks import issue_token
from app.md import patch as md_patch
from app.schemas import MdPatchSource


def src(conf: float = 0.95, raw: str = "心内科改到四楼东侧") -> MdPatchSource:
    return MdPatchSource(channel="voice", raw_utterance=raw, asr_confidence=conf)


def test_low_confidence_blocked(reset_departments):
    """门 1：置信度 < 0.85 不生成提案。"""
    with pytest.raises(md_patch.MdError) as ei:
        md_patch.propose("dept-cardio", "location", "门诊楼 4F 东侧", src(0.62))
    assert ei.value.code == "E_LOW_CONFIDENCE"


def test_field_not_allowed(reset_departments):
    """门 2：只允许四个值字段（科室名/ID 不可改）。"""
    with pytest.raises(md_patch.MdError) as ei:
        md_patch.propose("dept-cardio", "department_id", "dept-hack", src())
    assert ei.value.code == "E_FIELD_NOT_ALLOWED"


def test_section_not_found(reset_departments):
    """门 3：科室必须唯一命中。"""
    with pytest.raises(md_patch.MdError) as ei:
        md_patch.propose("dept-nonexistent", "location", "X", src())
    assert ei.value.code == "E_DEPT_NOT_FOUND"


def test_no_change_rejected(reset_departments):
    with pytest.raises(md_patch.MdError) as ei:
        md_patch.propose("dept-cardio", "location", "门诊楼 3F 东侧", src())
    assert ei.value.code == "E_NO_CHANGE"


def test_diff_contains_before_and_after(reset_departments):
    """确认弹层要展示的 diff 必须能看出差异（否则等于没确认）。"""
    p = md_patch.propose("dept-cardio", "location", "门诊楼 4F 东侧", src())
    assert "门诊楼 3F 东侧" in p.diff
    assert "门诊楼 4F 东侧" in p.diff
    assert p.before_value == "门诊楼 3F 东侧"
    assert p.after_value == "门诊楼 4F 东侧"
    assert p.base_hash != p.after_hash


def test_commit_without_token_rejected(reset_departments):
    """门 5：无/错令牌直接拒绝，且文件不变。"""
    p = md_patch.propose("dept-cardio", "location", "门诊楼 4F 东侧", src())
    before = md_patch.settings.departments_md.read_text(encoding="utf-8")
    r = md_patch.commit(p.patch_id, "deadbeef", "admin")
    assert r.ok is False
    assert r.error_code == "E_NO_TOKEN"
    assert md_patch.settings.departments_md.read_text(encoding="utf-8") == before


def test_commit_success_and_verified(reset_departments):
    """门 6：写入后重读校验必须通过。"""
    p = md_patch.propose("dept-cardio", "location", "门诊楼 4F 东侧", src())
    token = issue_token(p.patch_id, p.after_hash, p.created_at)
    r = md_patch.commit(p.patch_id, token, "admin")
    assert r.ok and r.verified
    text = md_patch.settings.departments_md.read_text(encoding="utf-8")
    assert "门诊楼 4F 东侧" in text
    assert "门诊楼 3F 东侧" not in text


def test_stale_base_blocked(reset_departments):
    """门 4：提案后文件被他人改动 → 拒绝并标记 expired。"""
    p = md_patch.propose("dept-cardio", "location", "门诊楼 4F 东侧", src())
    token = issue_token(p.patch_id, p.after_hash, p.created_at)
    # 模拟他人改动
    md_patch.settings.departments_md.write_text(
        md_patch.settings.departments_md.read_text(encoding="utf-8") + "\n<!-- 他人改动 -->\n",
        encoding="utf-8",
    )
    r = md_patch.commit(p.patch_id, token, "admin")
    assert r.ok is False
    assert r.error_code == "E_STALE_BASE"


def test_commit_idempotent(reset_departments):
    """重复提交返回首次结果，不重复写文件。"""
    p = md_patch.propose("dept-cardio", "location", "门诊楼 4F 东侧", src())
    token = issue_token(p.patch_id, p.after_hash, p.created_at)
    r1 = md_patch.commit(p.patch_id, token, "admin")
    r2 = md_patch.commit(p.patch_id, token, "admin")
    assert r1.ok and r2.ok
    assert r2.content_hash == r1.content_hash


def test_rollback(reset_departments):
    p = md_patch.propose("dept-cardio", "location", "门诊楼 4F 东侧", src())
    token = issue_token(p.patch_id, p.after_hash, p.created_at)
    r = md_patch.commit(p.patch_id, token, "admin")
    assert r.ok
    back = md_patch.rollback(r.version_id, "admin")
    assert back.ok
    text = md_patch.settings.departments_md.read_text(encoding="utf-8")
    assert "门诊楼 3F 东侧" in text
