"""运行时装配：适配器、熔断器、工具注册。

唯一装配点。新增数据源/工具都在这里接线，其他模块不感知具体实现。
"""
from __future__ import annotations

import time
from typing import Any

from . import state
from .datasource import dept_md
from .datasource.base import AdapterRegistry, CircuitBreaker
from .datasource.doctor_api import DoctorApiAdapter
from .datasource.schedule_api import ScheduleApiAdapter
from .datasource.schedule_manual import ManualScheduleAdapter
from .media import photo_ref_for
from .registry import ToolRegistry, ToolSpec, registry
from .schemas import (
    Candidate,
    DoctorBrief,
    ResolvedSchedule,
    ScheduleRow,
    SourceQuery,
    ToolResult,
)
from .settings import settings

adapters = AdapterRegistry()
breaker = CircuitBreaker(
    name="schedule-api",
    failure_threshold=int(settings.breaker_cfg().get("failure_threshold", 3)),
    open_ms=int(settings.breaker_cfg().get("open_ms", 60000)),
)

schedule_api = ScheduleApiAdapter()
schedule_manual = ManualScheduleAdapter()
doctor_api = DoctorApiAdapter()


def now_ms() -> int:
    return int(time.time() * 1000)


def today_str() -> str:
    return time.strftime("%Y-%m-%d")


async def decorate(rs: ResolvedSchedule) -> ResolvedSchedule:
    """给排班行补医生预览与头像，供 UI 直接渲染。"""
    for row in rs.rows:
        doctors = await doctor_api.by_department(row.department_id)
        preview: list[DoctorBrief] = []
        for d in doctors[:3]:
            ref = photo_ref_for(d.doctor_id)
            preview.append(
                DoctorBrief(
                    doctor_id=d.doctor_id,
                    name=d.name,
                    title=d.title,
                    photo_url=ref["url_192"] if ref else None,
                )
            )
        row.doctors_preview = preview
        if not row.doctor_count and doctors:
            row.doctor_count = len(doctors)
    return rs


# ─────────────────────────── 工具实现 ───────────────────────────


async def _tool_list_on_duty(args: dict[str, Any]) -> ToolResult:
    from .policy import resolve_all

    day = args.get("date") or today_str()
    rs = await resolve_all(day, api=schedule_api, manual=schedule_manual, breaker=breaker)
    rs = await decorate(rs)
    return ToolResult(status="ok", data=rs, source=rs.meta,
                      reason=rs.reason, degraded_to=("manual" if rs.degraded else None))


async def _tool_get_department(args: dict[str, Any]) -> ToolResult:
    depts = dept_md.load_all(settings.departments_md)
    key = str(args.get("name_or_id") or "")
    hit = dept_md.find(depts, key)
    if hit:
        return ToolResult(status="ok", data=hit)
    cands = [Candidate(id=d.department_id, name=d.name, score=s, kind="dept")
             for d, s in dept_md.search(depts, key)]
    return ToolResult(status="not_found", candidates=cands)


async def _tool_search_departments(args: dict[str, Any]) -> ToolResult:
    depts = dept_md.load_all(settings.departments_md)
    limit = int(args.get("limit") or 5)
    cands = [Candidate(id=d.department_id, name=d.name, score=s, kind="dept")
             for d, s in dept_md.search(depts, str(args.get("query") or ""), limit)]
    return ToolResult(status="ok", data=cands)


async def _tool_get_doctor(args: dict[str, Any]) -> ToolResult:
    key = str(args.get("name_or_id") or "").strip()
    doc = await doctor_api.by_id(key)
    if doc is None:
        named = await doctor_api.by_name(key)
        if len(named) == 1:
            doc = named[0]
        elif len(named) > 1:
            cands = [Candidate(id=d.doctor_id, name=d.name, score=1.0, kind="doctor") for d in named]
            return ToolResult(status="not_found", candidates=cands)
    if doc is None:
        return ToolResult(status="not_found", candidates=[])
    ref = photo_ref_for(doc.doctor_id)
    if ref:
        from .schemas import DoctorPhotoRef

        doc.photo = DoctorPhotoRef(**ref)
    return ToolResult(status="ok", data=doc)


async def _tool_get_doctor_schedule(args: dict[str, Any]) -> ToolResult:
    doc = await doctor_api.by_id(str(args.get("doctor_id") or ""))
    if doc is None:
        return ToolResult(status="not_found", candidates=[])
    day = args.get("date") or today_str()
    slots = [s for s in doc.schedule if s.date in ("*", day)]
    return ToolResult(status="ok", data={"doctor": doc, "slots": slots})


async def _tool_get_doctor_photo(args: dict[str, Any]) -> ToolResult:
    ref = photo_ref_for(str(args.get("doctor_id") or ""))
    return ToolResult(status="ok", data=ref)


def build_runtime() -> None:
    adapters.register(schedule_api)
    adapters.register(schedule_manual)
    adapters.register(doctor_api)

    registry.register(ToolSpec("list_on_duty", "查询某天出诊科室清单", _tool_list_on_duty))
    registry.register(ToolSpec("get_department", "按名称或 id 查科室详情（位置/诊室/服务范围/电话）",
                               _tool_get_department))
    registry.register(ToolSpec("search_departments", "模糊搜索科室，返回候选", _tool_search_departments))
    registry.register(ToolSpec("get_doctor", "查医生详情（职称/专长/坐诊时间）", _tool_get_doctor))
    registry.register(ToolSpec("get_doctor_schedule", "查医生坐诊时间", _tool_get_doctor_schedule))
    registry.register(ToolSpec("get_doctor_photo", "查医生头像地址", _tool_get_doctor_photo))

    # 写类工具仅登记（披露与授权由 hooks 控制），实现由 admin API 直接调用，避免语音通道触达
    async def _denied(_: dict[str, Any]) -> ToolResult:
        return ToolResult(status="denied", reason="WRITE_TOOL_REQUIRES_ADMIN_API")

    registry.register(ToolSpec("propose_md_patch", "（管理端）生成 md 修改提案，不落盘", _denied))
    registry.register(ToolSpec("commit_md_patch", "（管理端）提交 md 修改，需确认令牌", _denied))


__all__ = [
    "adapters", "breaker", "build_runtime", "decorate", "doctor_api",
    "now_ms", "registry", "schedule_api", "schedule_manual", "today_str",
]
