"""V 层：grounding 硬门禁。

规则：回答里出现的每个事实字段值，必须能在工具结果里找到原文。
- 通过 → 允许输出（可能是 LLM 润色后的句子）；
- 不通过 → **丢弃模型文本，改用模板原文**（不是重试，重试只会产生另一个幻觉）。
"""
from __future__ import annotations

import re

from .schemas import FieldCheck, GroundingReport

_WS = re.compile(r"[\s，。、；：,.;:（）()\-]+")


def _canon(s: str) -> str:
    return _WS.sub("", s or "")


def grounding_check(answer_text: str, facts: dict[str, str]) -> GroundingReport:
    """facts: {字段名: 期望出现在回答中的值}。空值跳过（没有的事实不检查）。"""
    canon_answer = _canon(answer_text)
    checks: list[FieldCheck] = []
    missing: list[str] = []

    for field, value in facts.items():
        if not value:
            continue
        cand = _canon(str(value))
        ok = cand in canon_answer
        checks.append(FieldCheck(field=field, claimed=str(value), actual=str(value), match=ok))
        if not ok:
            missing.append(field)

    return GroundingReport(pass_=not missing, checked_fields=checks, missing=missing)
