"""手动排班适配器：`data/schedule/manual.md` → ScheduleRow[]。

表格列：科室ID | 科室 | 院区 | 位置 | 医生数 | 状态 | 生效日 | 失效日
生效日/失效日留空 = 长期有效；日期格式 YYYY-MM-DD（含端点）。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..schemas import ScheduleRow, SourceQuery, SourceResult
from ..settings import settings

_SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")


def split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def parse_table(text: str) -> list[dict[str, str]]:
    """解析 md 中的第一个表格为 dict 列表。"""
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []
    headers = split_row(lines[0])
    rows: list[dict[str, str]] = []
    for ln in lines[1:]:
        if _SEP_RE.match(ln.strip()):
            continue
        cells = split_row(ln)
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, cells)))
    return rows


def _in_range(day: str, valid_from: str, valid_to: str) -> bool:
    if valid_from and day < valid_from:
        return False
    if valid_to and day > valid_to:
        return False
    return True


def load_rows(day: str) -> list[ScheduleRow]:
    path: Path = settings.manual_schedule_md
    if not path.is_file():
        return []
    rows: list[ScheduleRow] = []
    for r in parse_table(path.read_text(encoding="utf-8")):
        if not _in_range(day, r.get("生效日", ""), r.get("失效日", "")):
            continue
        dept_id = r.get("科室ID", "").strip()
        if not dept_id:
            continue
        try:
            cnt = int(r.get("医生数", "0") or 0)
        except ValueError:
            cnt = 0
        status = "suspended" if r.get("状态", "").strip() in {"停诊", "suspended", "off"} else "on_duty"
        rows.append(
            ScheduleRow(
                department_id=dept_id,
                department_name=r.get("科室", "").strip(),
                campus_id=r.get("院区", "本部").strip() or "本部",
                location=r.get("位置", "").strip(),
                doctor_count=cnt,
                status=status,
                source="manual",
                fetched_at=int(time.time() * 1000),
            )
        )
    return rows


class ManualScheduleAdapter:
    id = "schedule-manual"
    kind = "schedule"

    async def health(self) -> dict:
        return {"id": self.id, "ok": settings.manual_schedule_md.is_file(),
                "detail": str(settings.manual_schedule_md)}

    async def fetch(self, q: SourceQuery) -> SourceResult:
        now = int(time.time() * 1000)
        try:
            rows = load_rows(q.date)
        except Exception:  # noqa: BLE001 — 解析失败要作为观察回喂（error_code），不能吞成空列表
            return SourceResult(ok=False, fetched_at=now, error_code="PARSE_ERROR", rows=[])
        return SourceResult(ok=True, rows=rows, fetched_at=now)
