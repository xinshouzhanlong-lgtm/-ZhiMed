"""E 层：执行循环（分类与执行 + 双通道）。

纪律（对应设计文档 §4.1）：
- 终止条件由 grounding 裁定，不由模型自评；
- 三道保险：工具调用上限 3、token 预算、总超时；
- 打转检测：同一指纹连续 2 次同结果/同失败 → 打断转兜底；
- 异常一律不外抛，顶层兜底成 TurnOutput（患者端不能白屏）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

from . import evaluate, intent as intent_mod, llm, state
from .datasource import dept_md
from .hooks import hooks
from .runtime import doctor_api, now_ms, registry, today_str
from .schemas import (
    AnswerCard,
    IntentKind,
    LatencyBreakdown,
    ResolvedSchedule,
    Slots,
    TurnContext,
    TurnInput,
    TurnOutput,
)
from .settings import settings

_TURN_CACHE: dict[str, list[str]] = {}


def _fp(intent: str, slots: Slots) -> str:
    raw = json.dumps({"i": intent, "d": slots.department_id, "c": slots.doctor_id},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _fmt_slots(slots: list[Any]) -> str:
    return "、".join(f"{s.period} {s.room}" for s in slots) if slots else "暂无排班信息"


async def handle_turn(input_: TurnInput, ctx: TurnContext) -> TurnOutput:
    started = now_ms()
    answer_id = uuid.uuid4().hex

    # 门：限流
    gate = hooks.run("pre_invocation", {"device_id": input_.device_id})
    if not gate.allow:
        return _wrap(answer_id, IntentKind.OUT_OF_SCOPE, "请求过于频繁，请稍后再试。",
                     degraded=False, path="fallback", started=started, reason=gate.reason)

    raw = (input_.text or "").strip()
    if not raw:
        return _wrap(answer_id, IntentKind.OUT_OF_SCOPE, "我没有听清，请再说一次。",
                     degraded=False, path="fallback", started=started)

    session = state.load_session(input_.session_id) if input_.session_id else None

    try:
        async with asyncio.timeout(float(settings.limits.get("turn_timeout_ms", 6000)) / 1000):
            intent, slots, path = await intent_mod.classify(raw, session)

            # 打转检测：同指纹连续 2 次 → 打断
            fp = _fp(intent.value, slots)
            hist = _TURN_CACHE.setdefault(input_.device_id, [])
            if len(hist) >= 2 and hist[-1] == hist[-2] == fp:
                return _wrap(answer_id, intent, "这个问题我暂时查不到更准确的信息，建议咨询导医台。",
                             degraded=False, path="fallback", started=started, reason="LOOP_GUARD")
            hist.append(fp)
            del hist[:-6]

            out = await _execute(answer_id, intent, slots, path, raw, ctx, started)

        state.save_session(
            input_.session_id or answer_id, input_.device_id, intent.value,
            slots.department_id, slots.doctor_id,
        )
        state.log_query(input_.session_id, raw, intent.value, slots.model_dump(), path,
                        out.latency.model_dump())
        state.save_answer(answer_id, intent.value, out.display_text,
                          [r.model_dump() for r in registry.records[-3:]],
                          out.__dict__.get("_grounded", []), True,
                          _source_version())
        return out
    except (TimeoutError, asyncio.TimeoutError):
        return _wrap(answer_id, IntentKind.OUT_OF_SCOPE, "查询超时了，请再试一次或咨询导医台。",
                     degraded=False, path="fallback", started=started, reason="TIMEOUT")
    except Exception as exc:  # noqa: BLE001 — 顶层兜底，绝不外抛
        state.add_badcase(raw, f"EXCEPTION:{type(exc).__name__}", "", str(exc)[:200])
        return _wrap(answer_id, IntentKind.OUT_OF_SCOPE, "系统繁忙，请稍后再试或咨询导医台。",
                     degraded=False, path="fallback", started=started)


def _source_version() -> str:
    from .datasource.dept_md import content_hash, read_text

    try:
        return content_hash(read_text(settings.departments_md))
    except Exception:  # noqa: BLE001
        return ""


async def _execute(answer_id: str, intent: IntentKind, slots: Slots, path: str, raw: str,
                   ctx: TurnContext, started: int) -> TurnOutput:
    tool_ms = 0

    # ── 医疗红线：直接阻断，不查数据 ──
    if intent is IntentKind.MEDICAL_REDLINE:
        text = ("这个问题涉及诊断或用药，我不能给出判断。请到相应科室由医生面诊，"
                "如情况紧急请立即前往急诊或拨打 120。")
        return _wrap(answer_id, intent, text, degraded=False, path=path, started=started)

    if intent is IntentKind.OUT_OF_SCOPE:
        text = ("我可以帮您查：今天有哪些科室出诊、科室的位置与诊室、科室服务范围、"
                "医生的职称专长与坐诊时间。您想了解哪一个？")
        return _wrap(answer_id, intent, text, degraded=False, path=path, started=started)

    # ── 今日出诊 ──
    if intent is IntentKind.ASK_TODAY_DUTY:
        r = await registry.invoke("patient_voice", "list_on_duty", {"date": today_str()})
        tool_ms = now_ms() - started
        if r.status != "ok" or r.data is None:
            return _wrap(answer_id, intent, "排班信息暂时取不到，请稍后再试或咨询导医台。",
                         degraded=True, path=path, started=started, reason=r.reason)
        rs: ResolvedSchedule = r.data
        on_duty = [row for row in rs.rows if row.status == "on_duty"]
        if not on_duty:
            return _wrap(answer_id, intent, "今天暂时没有查询到出诊科室。",
                         degraded=rs.degraded, path=path, started=started, reason=rs.reason)
        names = "、".join(f"{row.department_name}（{row.location}）" for row in on_duty)
        text = f"今天共有 {len(on_duty)} 个科室出诊：{names}。"
        if rs.degraded:
            text = "（当前为人工维护数据，可能不是最新）" + text
        cards = [
            AnswerCard(kind="dept", title=row.department_name,
                       subtitle=f"{row.location} · {row.doctor_count} 位医生",
                       actions=["看详情", "看医生"],
                       payload={"department_id": row.department_id})
            for row in on_duty
        ]
        return _wrap(answer_id, intent, text, degraded=rs.degraded, path=path,
                     started=started, cards=cards, source_meta=rs.meta, reason=rs.reason,
                     facts={f"科室{i}": row.department_name for i, row in enumerate(on_duty)},
                     tool_ms=tool_ms)

    # ── 科室维度 ──
    if intent in {IntentKind.ASK_DEPT_LOCATION, IntentKind.ASK_DEPT_SERVICE,
                  IntentKind.ASK_DEPT_CONTACT}:
        if not slots.department_id:
            return _wrap(answer_id, intent, "您想了解哪个科室？可以说科室名，例如心血管内科。",
                         degraded=False, path=path, started=started)
        r = await registry.invoke("patient_voice", "get_department",
                                  {"name_or_id": slots.department_id})
        tool_ms = now_ms() - started
        if r.status == "not_found":
            names = "、".join(c.name for c in r.candidates[:3]) or "没有匹配的科室"
            return _wrap(answer_id, intent, f"没有找到这个科室，您是想问：{names}？",
                         degraded=False, path=path, started=started,
                         candidates=r.candidates, reason="NOT_FOUND")
        dept = r.data
        if intent is IntentKind.ASK_DEPT_SERVICE:
            text = f"{dept.name}的服务范围：{dept.service_scope}。"
            facts = {"科室": dept.name, "服务范围": dept.service_scope}
        elif intent is IntentKind.ASK_DEPT_CONTACT:
            text = f"{dept.name}的联系电话是 {dept.phone or '暂未提供'}。"
            facts = {"科室": dept.name, "电话": dept.phone or "暂未提供"}
        else:
            text = f"{dept.name}在{dept.location}，诊室是 {dept.rooms}。"
            facts = {"科室": dept.name, "位置": dept.location, "诊室": dept.rooms}
        card = AnswerCard(kind="dept", title=dept.name,
                          subtitle=f"{dept.location} · {dept.rooms}",
                          actions=["导航", "看医生"],
                          payload={"department_id": dept.department_id})
        return await _finish(answer_id, intent, text, facts, path, started, [card], tool_ms,
                             prefix_source=False)

    # ── 医生维度 ──
    if intent is IntentKind.ASK_DOCTOR_PROFILE:
        if slots.doctor_id:
            r = await registry.invoke("patient_voice", "get_doctor", {"name_or_id": slots.doctor_id})
            tool_ms = now_ms() - started
            if r.status != "ok" or r.data is None:
                return _wrap(answer_id, IntentKind.OUT_OF_SCOPE, "没有找到这位医生，请确认姓名。",
                             degraded=False, path=path, started=started, reason="NOT_FOUND")
            doc = r.data
            slots_txt = _fmt_slots([s for s in doc.schedule if s.date == "*" or s.date == today_str()])
            text = f"{doc.name}，{doc.title}，专长{doc.specialty}。坐诊时间：{slots_txt}。"
            facts = {"姓名": doc.name, "职称": doc.title, "专长": doc.specialty}
            card = AnswerCard(kind="doctor", title=doc.name, subtitle=f"{doc.title} · {doc.specialty}",
                              photo_url=(doc.photo.url_192 if doc.photo else None),
                              actions=["看坐诊时间"],
                              payload={"doctor_id": doc.doctor_id})
            return await _finish(answer_id, intent, text, facts, path, started, [card], tool_ms)

        # 科室下的医生清单
        dept_id = slots.department_id
        if not dept_id:
            return _wrap(answer_id, intent, "您想了解哪个科室的医生？",
                         degraded=False, path=path, started=started)
        depts = dept_md.load_all(settings.departments_md)
        dept = dept_md.find(depts, dept_id)
        doctors = await doctor_api.by_department(dept_id)
        tool_ms = now_ms() - started
        if not doctors:
            return _wrap(answer_id, intent, f"{dept.name if dept else '该科室'}今天没有查询到出诊医生。",
                         degraded=False, path=path, started=started, reason="EMPTY")
        parts = []
        cards = []
        from .media import photo_ref_for

        for d in doctors[:6]:
            slots_txt = _fmt_slots([s for s in d.schedule if s.date == "*" or s.date == today_str()])
            parts.append(f"{d.name}（{d.title}、{d.specialty}、{slots_txt}）")
            ref = photo_ref_for(d.doctor_id)
            cards.append(AnswerCard(kind="doctor", title=d.name,
                                    subtitle=f"{d.title} · {d.specialty}",
                                    photo_url=(ref["url_192"] if ref else None),
                                    actions=["看详情"], payload={"doctor_id": d.doctor_id}))
        text = f"{dept.name if dept else '该科室'}有 {len(doctors)} 位医生：{'；'.join(parts)}。"
        return _wrap(answer_id, intent, text, degraded=False, path=path, started=started,
                     cards=cards, tool_ms=tool_ms)

    if intent is IntentKind.ASK_DOCTOR_SCHEDULE:
        if not slots.doctor_id:
            return _wrap(answer_id, intent, "您想问哪位医生的坐诊时间？",
                         degraded=False, path=path, started=started)
        r = await registry.invoke("patient_voice", "get_doctor_schedule",
                                  {"doctor_id": slots.doctor_id, "date": today_str()})
        tool_ms = now_ms() - started
        if r.status != "ok":
            return _wrap(answer_id, intent, "没有查询到这位医生的坐诊时间。",
                         degraded=False, path=path, started=started, reason="NOT_FOUND")
        doc = r.data["doctor"]
        slots_list = r.data["slots"]
        text = f"{doc.name}（{doc.title}）的坐诊时间：{_fmt_slots(slots_list)}。"
        facts = {"姓名": doc.name, "坐诊": slots_list[0].room if slots_list else ""}
        card = AnswerCard(kind="doctor", title=doc.name, subtitle=f"{doc.title} · {doc.specialty}",
                          actions=["看详情"], payload={"doctor_id": doc.doctor_id})
        return await _finish(answer_id, intent, text, facts, path, started, [card], tool_ms)

    # ── 导航 ──
    if intent is IntentKind.NAVIGATE:
        return _wrap(answer_id, intent, "请告诉我您要去的科室，我帮您查位置。",
                     degraded=False, path=path, started=started)

    return _wrap(answer_id, IntentKind.OUT_OF_SCOPE, "我暂时无法回答这个问题，建议咨询导医台。",
                 degraded=False, path=path, started=started)


async def _finish(answer_id: str, intent: IntentKind, template_text: str, facts: dict[str, str],
                  path: str, started: int, cards: list[AnswerCard], tool_ms: int,
                  prefix_source: bool = False) -> TurnOutput:
    """润色 → grounding → 不通过就退回模板。"""
    text = template_text
    polished = await llm.polish(template_text, facts)
    if polished:
        report = evaluate.grounding_check(polished, facts)
        if report.pass_:
            text = polished
        else:
            state.add_badcase(template_text, f"GROUNDING_FAIL:{','.join(report.missing)}",
                              template_text, polished)
    return _wrap(answer_id, intent, text, degraded=False, path=path, started=started,
                 cards=cards, tool_ms=tool_ms, facts=facts)


def _wrap(answer_id: str, intent: IntentKind, text: str, *, degraded: bool, path: str,
          started: int, cards: list[AnswerCard] | None = None, source_meta: Any = None,
          reason: str | None = None, facts: dict[str, str] | None = None,
          tool_ms: int = 0, candidates: list[Any] | None = None) -> TurnOutput:
    total = now_ms() - started
    out = TurnOutput(
        answer_id=answer_id,
        intent=intent.value,
        text=text,
        display_text=text,
        cards=cards or [],
        candidates=candidates or [],
        source_meta=source_meta,
        degraded=degraded,
        disclaimer=settings.disclaimer,
        latency=LatencyBreakdown(infer_ms=max(total - tool_ms, 0), tool_ms=tool_ms, total_ms=total),
        path=path,  # type: ignore[arg-type]
    )
    if facts:
        out.__dict__["_grounded"] = list(facts.keys())
    return out
