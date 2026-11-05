"""意图识别：规则直答通道 + 模型抽取通道 + 兜底。

通道 A（规则直答）：命中关键词表 + 实体 → 不进 LLM，path="fast"，延迟 < 30ms。
通道 B（模型抽取）：规则只认出意图但缺实体时，请 LLM 抽槽位；LLM 关闭则退回规则结果。
兜底：都不命中 → OUT_OF_SCOPE，由 loop 输出引导话术（不编造）。
"""
from __future__ import annotations

import re
from typing import Any

from .datasource import dept_md
from .schemas import IntentKind, RawEntity, Slots
from .settings import settings

REDLINE = ("吃什么药", "用什么药", "开点药", "开药", "处方", "诊断", "我得了", "是不是癌",
           "帮我挂号", "挂个号", "能不能治", "严重吗")
TODAY = ("今天", "今日", "现在", "当前", "有哪些科室", "哪些科", "出诊科室", "排班")
DOCTOR_LIST = ("哪些医生", "有谁", "哪个医生", "医生有哪些", "医生列表", "哪些专家")
SCHEDULE_KW = ("坐诊", "出诊时间", "什么时候", "哪天", "周几", "门诊时间", "几点")
LOCATION_KW = ("在哪", "几楼", "位置", "怎么走", "怎么去", "哪个楼", "哪里")
SERVICE_KW = ("服务范围", "看什么", "治什么", "擅长", "能看", "看哪些", "主治")
CONTACT_KW = ("电话", "联系", "号码", "咨询")

_STRIP = re.compile(r"[\s，。？！,.?!、]+")


def _norm(text: str) -> str:
    return _STRIP.sub("", text or "")


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def match_dept(text: str) -> str | None:
    """在文本中找科室（名称或别名）。取最长命中，避免"心内"抢过"心血管内科"。"""
    best: tuple[int, str] | None = None
    for d in dept_md.load_all(settings.departments_md):
        for cand in [d.name, *d.aliases]:
            if cand and cand in text:
                if best is None or len(cand) > best[0]:
                    best = (len(cand), d.department_id)
    return best[1] if best else None


def match_doctor(text: str, doctors: list[Any]) -> str | None:
    best: tuple[int, str] | None = None
    for doc in doctors:
        if doc.name and doc.name in text:
            if best is None or len(doc.name) > best[0]:
                best = (len(doc.name), doc.doctor_id)
    return best[1] if best else None


async def classify(text: str, session: dict | None = None) -> tuple[IntentKind, Slots, str]:
    """返回 (意图, 槽位, 通道)。通道取值 fast / model / fallback。"""
    from .runtime import doctor_api

    raw = (text or "").strip()
    norm = _norm(raw)
    slots = Slots()

    # ① 医疗红线优先（独立于模型，先拦）
    if _has(norm, REDLINE):
        return IntentKind.MEDICAL_REDLINE, slots, "fast"

    doctors = await doctor_api.all() or []
    dept_id = match_dept(norm)
    doctor_id = match_doctor(norm, doctors)
    slots.department_id = dept_id
    slots.doctor_id = doctor_id
    if dept_id:
        slots.raw_entities.append(RawEntity(type="dept", text=dept_id, confidence=1.0))

    # ② 指代继承：最近 3 轮内的科室/医生（只继承，不猜）
    inherited = False
    if dept_id is None and session and session.get("last_dept_id"):
        if _has(norm, DOCTOR_LIST + SERVICE_KW + LOCATION_KW + CONTACT_KW):
            slots.department_id = session["last_dept_id"]
            dept_id = slots.department_id
            inherited = True
    if doctor_id is None and session and session.get("last_doctor_id"):
        if _has(norm, SCHEDULE_KW + SERVICE_KW) and not any(d.name in norm for d in doctors):
            slots.doctor_id = session["last_doctor_id"]
            doctor_id = slots.doctor_id
            inherited = True

    # ③ 规则判意图
    intent = _rule_intent(norm, dept_id, doctor_id, inherited)

    if intent is not None:
        # 实体齐全 → 直答；缺实体 → 先请模型补槽
        need_entity = intent in {IntentKind.ASK_DEPT_LOCATION, IntentKind.ASK_DEPT_SERVICE,
                                 IntentKind.ASK_DEPT_CONTACT, IntentKind.ASK_DOCTOR_PROFILE,
                                 IntentKind.ASK_DOCTOR_SCHEDULE}
        if not need_entity or dept_id or doctor_id:
            return intent, slots, "fast"
        filled = await _model_fill(raw, doctors)
        if filled:
            slots.department_id = filled.get("department_id") or slots.department_id
            slots.doctor_id = filled.get("doctor_id") or slots.doctor_id
            if slots.department_id or slots.doctor_id:
                return intent, slots, "model"
        return intent, slots, "model"

    # ④ 规则未命中 → 模型抽取；LLM 关闭则兜底
    filled = await _model_fill(raw, doctors)
    if filled and filled.get("intent"):
        try:
            intent = IntentKind(filled["intent"])
        except ValueError:
            return IntentKind.OUT_OF_SCOPE, slots, "fallback"
        slots.department_id = filled.get("department_id") or slots.department_id
        slots.doctor_id = filled.get("doctor_id") or slots.doctor_id
        return intent, slots, "model"
    return IntentKind.OUT_OF_SCOPE, slots, "fallback"


def _rule_intent(norm: str, dept_id: str | None, doctor_id: str | None,
                 inherited: bool) -> IntentKind | None:
    if doctor_id:
        if _has(norm, SCHEDULE_KW):
            return IntentKind.ASK_DOCTOR_SCHEDULE
        return IntentKind.ASK_DOCTOR_PROFILE
    if dept_id:
        if _has(norm, DOCTOR_LIST):
            return IntentKind.ASK_DOCTOR_PROFILE      # 科室下的医生清单
        if _has(norm, CONTACT_KW):
            return IntentKind.ASK_DEPT_CONTACT
        if _has(norm, SERVICE_KW):
            return IntentKind.ASK_DEPT_SERVICE
        if _has(norm, LOCATION_KW) or inherited:
            return IntentKind.ASK_DEPT_LOCATION
        return IntentKind.ASK_DEPT_LOCATION           # 只提科室名 → 默认回答位置
    # "今天"单独出现不足以判出诊（"今天天气怎么样"不该被当成排班查询）
    if _has(norm, TODAY) and _has(norm, ("出诊", "排班", "科室", "门诊", "哪些科")):
        return IntentKind.ASK_TODAY_DUTY
    if _has(norm, DOCTOR_LIST):
        return IntentKind.ASK_DOCTOR_PROFILE
    if _has(norm, SCHEDULE_KW):
        return IntentKind.ASK_DOCTOR_SCHEDULE
    return None


async def _model_fill(raw: str, doctors: list[Any]) -> dict[str, Any] | None:
    """请 LLM 抽槽位。关闭时返回 None（调用方走兜底），不抛异常。"""
    from . import llm

    if not llm.enabled():
        return None
    depts = dept_md.load_all(settings.departments_md)
    system = (
        "你是医院导诊的意图解析器。只输出 JSON，不要解释。字段："
        "intent ∈ {ASK_DEPT_LOCATION,ASK_DEPT_SERVICE,ASK_DEPT_CONTACT,ASK_DOCTOR_PROFILE,"
        "ASK_DOCTOR_SCHEDULE,ASK_TODAY_DUTY,NAVIGATE,OUT_OF_SCOPE,MEDICAL_REDLINE},"
        "department_id（只能取给定清单里的 id 或 null）, doctor_id（只能取给定清单或 null）。"
    )
    user = (
        f"科室清单：{[{'id': d.department_id, 'name': d.name, 'aliases': d.aliases} for d in depts]}\n"
        f"医生清单：{[{'id': d.doctor_id, 'name': d.name, 'dept': d.department_id} for d in doctors]}\n"
        f"用户问：{raw}"
    )
    data = await llm.complete_json(system, user)
    if not data:
        return None
    # 校验：id 必须在清单内，防止模型编造实体
    valid_dept = {d.department_id for d in depts}
    valid_doc = {d.doctor_id for d in doctors}
    if data.get("department_id") not in valid_dept:
        data["department_id"] = None
    if data.get("doctor_id") not in valid_doc:
        data["doctor_id"] = None
    return data
