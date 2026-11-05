"""数据来源四态决策（★ 核心逻辑，对应设计文档 §7.1）。

纯函数式：依赖通过参数注入，方便单测覆盖 4 态 × 熔断 × 冲突。
三种结果语义必须严格区分，不允许混为一谈：
  - 成功：rows 非空，meta.source 标明来源
  - 空但不降级（api_only 失败）：degraded=True + reason=API_UNAVAILABLE —— 界面要显示"暂无"而非过期数据
  - 降级：degraded=True + reason=BREAKER_OPEN/API_FAILED —— 界面必须黄条 + 语音必须声明
"""
from __future__ import annotations

import time
from typing import Any

from . import state
from .datasource.base import CircuitBreaker
from .schemas import ResolvedSchedule, ScheduleRow, SourceMeta, SourceMode, SourcePolicy, SourceQuery
from .settings import settings


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_policy(scope: str) -> SourcePolicy:
    raw: dict[str, Any] = settings.policy_for(scope)
    return SourcePolicy(
        scope=scope,
        mode=SourceMode(raw.get("mode", "api_then_manual")),
        timeout_ms=int(raw.get("timeout_ms", 2000)),
        max_stale_ms=int(raw.get("max_stale_ms", 21_600_000)),
        reason=raw.get("reason"),
        expires_at=raw.get("expires_at"),
    )


def _pick(rows: list[ScheduleRow], scope: str) -> list[ScheduleRow]:
    """按 scope 过滤并复制（复制是为了避免不同策略共享同一行对象互相污染 source）。"""
    if scope in ("*", "", None):
        return [r.model_copy() for r in rows]
    return [r.model_copy() for r in rows if r.department_id == scope]


def _ok(rows: list[ScheduleRow], source: str, fetched_at: int, max_stale_ms: int,
        degraded: bool = False, reason: str | None = None, overridden: bool = False,
        conflicts: list[str] | None = None) -> ResolvedSchedule:
    now = _now_ms()
    for r in rows:
        r.source = source  # type: ignore[assignment]
        r.fetched_at = fetched_at
        r.stale = (now - fetched_at) > max_stale_ms
    return ResolvedSchedule(
        rows=rows,
        meta=SourceMeta(source=source, fetched_at=fetched_at,  # type: ignore[arg-type]
                        stale=any(r.stale for r in rows), overridden=overridden),
        degraded=degraded,
        reason=reason,
        conflicts=conflicts or [],
    )


def _empty(reason: str, degraded: bool = True) -> ResolvedSchedule:
    return ResolvedSchedule(rows=[], meta=None, degraded=degraded, reason=reason, conflicts=[])


async def resolve_schedule(day: str, scope: str, *, api, manual, breaker: CircuitBreaker,
                           record_conflicts: bool = True) -> ResolvedSchedule:
    policy = build_policy(scope)
    manual_q = SourceQuery(date=day, scope=scope if scope != "*" else None)

    # ── api_only：失败时明确"暂无"，绝不静默降级 ──
    if policy.mode is SourceMode.API_ONLY:
        r = await api.fetch(SourceQuery(date=day, timeout_ms=policy.timeout_ms))
        if r.ok:
            breaker.record_success()
            state.record_source_health("schedule-api", True, 0, None)
            return _ok(_pick(r.rows, scope), "api", r.fetched_at, policy.max_stale_ms)
        breaker.record_failure()
        state.record_source_health("schedule-api", False, 0, r.error_code)
        return _empty("API_UNAVAILABLE")

    # ── manual_only ──
    if policy.mode is SourceMode.MANUAL_ONLY:
        m = await manual.fetch(manual_q)
        if m.ok:
            return _ok(_pick(m.rows, scope), "manual", m.fetched_at, policy.max_stale_ms)
        return _empty("MANUAL_PARSE_ERROR", degraded=True)

    # ── manual_override：人工裁决优先，过期自动失效 ──
    if policy.mode is SourceMode.MANUAL_OVERRIDE:
        if policy.expires_at and _now_ms() > policy.expires_at:
            return await resolve_schedule(day, "*", api=api, manual=manual, breaker=breaker,
                                          record_conflicts=record_conflicts)
        m = await manual.fetch(manual_q)
        if m.ok and _pick(m.rows, scope):
            return _ok(_pick(m.rows, scope), "manual", m.fetched_at, policy.max_stale_ms,
                       overridden=True)
        r = await api.fetch(SourceQuery(date=day, timeout_ms=policy.timeout_ms))
        if r.ok:
            return _ok(_pick(r.rows, scope), "api", r.fetched_at, policy.max_stale_ms,
                       degraded=True, reason="OVERRIDE_EMPTY")
        return _empty("OVERRIDE_EMPTY_AND_API_DOWN")

    # ── api_then_manual（默认）──
    if breaker.state == "open":
        m = await manual.fetch(manual_q)
        if m.ok:
            return _ok(_pick(m.rows, scope), "manual", m.fetched_at, policy.max_stale_ms,
                       degraded=True, reason="BREAKER_OPEN")
        return _empty("BREAKER_OPEN_AND_MANUAL_EMPTY")

    r = await api.fetch(SourceQuery(date=day, timeout_ms=policy.timeout_ms))
    if r.ok:
        breaker.record_success()
        state.record_source_health("schedule-api", True, 0, None)
        conflicts: list[str] = []
        if record_conflicts:
            m_for_cmp = await manual.fetch(manual_q)
            if m_for_cmp.ok:
                conflicts = _record_conflicts(day, r.rows, m_for_cmp.rows, winner="api")
        return _ok(_pick(r.rows, scope), "api", r.fetched_at, policy.max_stale_ms, conflicts=conflicts)

    breaker.record_failure()
    state.record_source_health("schedule-api", False, 0, r.error_code)
    m = await manual.fetch(manual_q)
    if m.ok:
        return _ok(_pick(m.rows, scope), "manual", m.fetched_at, policy.max_stale_ms,
                   degraded=True, reason=r.error_code or "API_FAILED")
    return _empty(f"{r.error_code or 'API_FAILED'}_AND_MANUAL_EMPTY")


def _record_conflicts(day: str, api_rows: list[ScheduleRow], manual_rows: list[ScheduleRow],
                      winner: str) -> list[str]:
    """两源同一科室字段不一致 → 落 data_conflict（不自动 resolve）。"""
    manual_map = {m.department_id: m for m in manual_rows}
    out: list[str] = []
    for a in api_rows:
        m = manual_map.get(a.department_id)
        if not m:
            continue
        if (a.location or "") != (m.location or "") or a.doctor_count != m.doctor_count:
            cid = state.record_conflict(
                day, a.department_id,
                f"location={a.location};doctor_count={a.doctor_count}",
                f"location={m.location};doctor_count={m.doctor_count}",
                winner,
            )
            out.append(cid)
    return out


async def resolve_all(day: str, *, api, manual, breaker: CircuitBreaker) -> ResolvedSchedule:
    """全量排班：以科室清单为全集，对每个科室按其自身 policy 逐个解析，再合并。

    这样 `api_only` / `manual_only` 这类科室级策略才能真正生效（而不是被全局策略覆盖）。
    """
    from .datasource import dept_md

    depts = dept_md.load_all(settings.departments_md)
    # 先用默认策略拿一次全量，得到每个科室可用的兜底数据
    base = await resolve_schedule(day, "*", api=api, manual=manual, breaker=breaker)
    merged: dict[str, ScheduleRow] = {}
    degraded_reasons: list[str] = []
    conflicts: list[str] = list(base.conflicts)

    manual_res = await manual.fetch(SourceQuery(date=day))
    manual_map = {r.department_id: r for r in manual_res.rows if manual_res.ok}
    api_res = await api.fetch(SourceQuery(date=day, timeout_ms=2000))
    api_map = {r.department_id: r for r in api_res.rows if api_res.ok}

    default_mode = build_policy("*").mode
    for d in depts:
        pol = build_policy(d.department_id)
        if pol.mode == default_mode:
            # 与全局一致 → 直接用 base 结果（避免 N 次重复请求）
            for row in base.rows:
                merged[row.department_id] = row
            continue
        sub = await resolve_schedule(day, d.department_id, api=api, manual=manual,
                                     breaker=breaker, record_conflicts=False)
        for row in sub.rows:
            merged[row.department_id] = row
        if not sub.rows:
            # 该科室策略下无数据：保留 base 里同科室的行（如果存在），否则不展示
            if d.department_id in {r.department_id for r in base.rows}:
                pass
            else:
                degraded_reasons.append(f"{d.name}:{sub.reason}")
        if sub.degraded:
            degraded_reasons.append(f"{d.name}:{sub.reason}")

    rows = list(merged.values())

    # 未在地区清单里的科室（HIS 多给的）也展示，避免漏诊室
    known = {d.department_id for d in depts}
    for rid, row in {**manual_map, **api_map}.items():
        if rid not in known and rid not in merged:
            rows.append(row)

    rows.sort(key=lambda r: (r.status != "on_duty", r.department_id))
    meta = base.meta
    return ResolvedSchedule(
        rows=rows,
        meta=meta,
        degraded=bool(degraded_reasons) or base.degraded,
        reason=("; ".join(dict.fromkeys(degraded_reasons)) if degraded_reasons else base.reason),
        conflicts=list(dict.fromkeys(conflicts)),
    )
