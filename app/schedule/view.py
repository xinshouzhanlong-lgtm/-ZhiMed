"""按日期/时段合并医院排班与管理员导入；停诊记录覆盖对应时段。"""
from __future__ import annotations

from ..schemas import DoctorSlot

PERIODS = ("上午", "下午", "夜间")


def day_slots(slots: list[DoctorSlot], day: str, *, include_suspended: bool = False) -> list[DoctorSlot]:
    chosen: dict[str, DoctorSlot] = {}
    for slot in slots:
        if slot.date not in ("*", day):
            continue
        previous = chosen.get(slot.period)
        if previous is None or (slot.source == "manual" or previous.date == "*"):
            chosen[slot.period] = slot
    return [chosen[p] for p in PERIODS if p in chosen
            and (include_suspended or chosen[p].status == "on_duty")]
