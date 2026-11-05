"""数据来源四态 × 熔断 × 冲突（文档 §7.1）。"""
from __future__ import annotations

import time

import pytest

from app import policy, state
from app.datasource.base import CircuitBreaker
from app.schemas import ScheduleRow, SourceQuery, SourceResult

DAY = "2026-10-20"


def row(dept_id: str, name: str, loc: str, cnt: int = 2) -> ScheduleRow:
    return ScheduleRow(department_id=dept_id, department_name=name, location=loc, doctor_count=cnt)


class FakeApi:
    id = "schedule-api"
    kind = "schedule"

    def __init__(self, ok=True, rows=None, error="HTTP_5XX"):
        self.ok = ok
        self.rows = rows if rows is not None else [row("dept-cardio", "心血管内科", "门诊楼 3F 东侧", 4)]
        self.error = error
        self.calls = 0

    async def health(self):
        return {"id": self.id, "ok": self.ok}

    async def fetch(self, q: SourceQuery) -> SourceResult:
        self.calls += 1
        if self.ok:
            return SourceResult(ok=True, rows=self.rows, fetched_at=int(time.time() * 1000))
        return SourceResult(ok=False, rows=[], fetched_at=int(time.time() * 1000), error_code=self.error)


class FakeManual:
    id = "schedule-manual"
    kind = "schedule"

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [row("dept-ortho", "骨科", "门诊楼 2F 南侧", 2)]

    async def health(self):
        return {"id": self.id, "ok": True}

    async def fetch(self, q: SourceQuery) -> SourceResult:
        return SourceResult(ok=True, rows=list(self.rows), fetched_at=int(time.time() * 1000))


@pytest.fixture()
def fresh_breaker():
    return CircuitBreaker("schedule-api", failure_threshold=3, open_ms=60_000)


def _patch_policy(monkeypatch, mode: str, scope: str = "*"):
    original = policy.settings.policy_for

    def fake_policy_for(s: str):
        base = original(s)
        if s == scope:
            base["mode"] = mode
        return base

    monkeypatch.setattr(policy.settings, "policy_for", fake_policy_for)


@pytest.mark.asyncio
async def test_api_only_success(monkeypatch, fresh_breaker):
    _patch_policy(monkeypatch, "api_only")
    api, manual = FakeApi(ok=True), FakeManual()
    rs = await policy.resolve_schedule(DAY, "*", api=api, manual=manual, breaker=fresh_breaker)
    assert rs.meta and rs.meta.source == "api"
    assert rs.degraded is False
    assert manual is not None


@pytest.mark.asyncio
async def test_api_only_failure_does_not_degrade(monkeypatch, fresh_breaker):
    """核心约束：api_only 失败必须"暂无"，绝不静默降级到 manual。"""
    _patch_policy(monkeypatch, "api_only")
    api, manual = FakeApi(ok=False), FakeManual()
    rs = await policy.resolve_schedule(DAY, "*", api=api, manual=manual, breaker=fresh_breaker)
    assert rs.rows == []
    assert rs.degraded is True
    assert rs.reason == "API_UNAVAILABLE"


@pytest.mark.asyncio
async def test_manual_only(monkeypatch, fresh_breaker):
    _patch_policy(monkeypatch, "manual_only")
    api, manual = FakeApi(ok=True), FakeManual()
    rs = await policy.resolve_schedule(DAY, "*", api=api, manual=manual, breaker=fresh_breaker)
    assert rs.meta and rs.meta.source == "manual"
    assert api.calls == 0, "manual_only 不应调用接口"


@pytest.mark.asyncio
async def test_api_then_manual_fallback(monkeypatch, fresh_breaker):
    _patch_policy(monkeypatch, "api_then_manual")
    api, manual = FakeApi(ok=False, error="TIMEOUT"), FakeManual()
    rs = await policy.resolve_schedule(DAY, "*", api=api, manual=manual, breaker=fresh_breaker)
    assert rs.meta and rs.meta.source == "manual"
    assert rs.degraded is True
    assert rs.reason == "TIMEOUT"


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold(monkeypatch, fresh_breaker):
    _patch_policy(monkeypatch, "api_then_manual")
    api, manual = FakeApi(ok=False), FakeManual()
    for _ in range(3):
        await policy.resolve_schedule(DAY, "*", api=api, manual=manual, breaker=fresh_breaker)
    assert fresh_breaker.state == "open"

    calls_before = api.calls
    rs = await policy.resolve_schedule(DAY, "*", api=api, manual=manual, breaker=fresh_breaker)
    assert api.calls == calls_before, "熔断打开时不应再打接口"
    assert rs.degraded is True
    assert rs.reason == "BREAKER_OPEN"


@pytest.mark.asyncio
async def test_manual_override(monkeypatch, fresh_breaker):
    _patch_policy(monkeypatch, "manual_override")
    api, manual = FakeApi(ok=True), FakeManual()
    rs = await policy.resolve_schedule(DAY, "*", api=api, manual=manual, breaker=fresh_breaker)
    assert rs.meta and rs.meta.source == "manual"
    assert rs.meta.overridden is True
    assert api.calls == 0


@pytest.mark.asyncio
async def test_conflict_recorded(monkeypatch, fresh_breaker):
    """两源同一科室位置不一致 → 落 data_conflict，且不自动 resolve。"""
    _patch_policy(monkeypatch, "api_then_manual")
    api = FakeApi(ok=True, rows=[row("dept-ortho", "骨科", "门诊楼 2F 北侧", 2)])
    manual = FakeManual(rows=[row("dept-ortho", "骨科", "门诊楼 2F 南侧", 2)])
    rs = await policy.resolve_schedule(DAY, "*", api=api, manual=manual, breaker=fresh_breaker)
    assert rs.conflicts, "应记录冲突"
    pending = [c for c in state.list_conflicts() if c["department_id"] == "dept-ortho"]
    assert pending and pending[0]["resolved_at"] == 0


@pytest.mark.asyncio
async def test_stale_flag(monkeypatch, fresh_breaker):
    _patch_policy(monkeypatch, "api_only")

    class OldApi(FakeApi):
        async def fetch(self, q):
            return SourceResult(ok=True, rows=[row("dept-cardio", "心血管内科", "3F")],
                                fetched_at=int(time.time() * 1000) - 30 * 3600 * 1000)

    rs = await policy.resolve_schedule(DAY, "*", api=OldApi(), manual=FakeManual(),
                                       breaker=fresh_breaker)
    assert rs.rows[0].stale is True
    assert rs.meta.stale is True
