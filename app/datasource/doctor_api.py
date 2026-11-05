"""★ HIS 医生信息接口适配器 —— 预留实现（本期不接真实接口）。

真实对接时只改两处：
    ① `_fetch_http()`  发起真实请求
    ② `_map_doctor()`  字段映射

预期数据契约（HIS → 本系统）
─────────────────────────────────────────────────────────────────
  请求：GET {base_url}?dept_code={dept_id}   （或 ?doctor_id=…）
  响应：
  {
    "code": 0, "msg": "ok",
    "data": { "list": [
      {
        "doctor_id":    "doc-zhangming",
        "name":         "张明",
        "title":        "主任医师",        # 主任医师/副主任医师/主治医师/住院医师
        "specialty":    "冠心病介入…",      # 专长
        "dept_code":    "dept-cardio",     # 与 departments.md 的 id 对齐
        "schedules": [                     # 坐诊时间
          {"date": "2026-10-20", "period": "上午", "room": "3F-12"}
        ]
      }
    ]}
  }
  注：`date` 允许为 "*" 表示"每周固定"，本期 fixture 用该写法降低维护成本。
"""
from __future__ import annotations

import time
from typing import Any

from ..schemas import DoctorProfile, DoctorSlot, SourceQuery, SourceResult
from ..settings import settings
from .base import load_fixture


class DoctorApiAdapter:
    """医生信息适配器。mode: off | fixture | http。"""

    id = "doctor-api"
    kind = "doctor"

    def __init__(self) -> None:
        cfg = settings.endpoint("doctor_api")
        self.mode: str = cfg.get("mode", "fixture")
        self.fixture: str = cfg.get("fixture", "data/fixtures/doctors.json")
        self.base_url: str = cfg.get("base_url", "")
        self.auth_env: str = cfg.get("auth_env", "GUIDE_HIS_APP_SECRET")
        self._cache: list[DoctorProfile] | None = None
        self._cache_at: int = 0

    async def health(self) -> dict[str, Any]:
        return {"id": self.id, "mode": self.mode, "configured": bool(self.base_url),
                "ok": self.mode != "off"}

    async def fetch(self, q: SourceQuery) -> SourceResult:
        now = int(time.time() * 1000)
        doctors = await self.all()
        if doctors is None:
            return SourceResult(ok=False, fetched_at=now, error_code="NOT_CONFIGURED")
        return SourceResult(ok=True, rows=doctors, fetched_at=now)

    # ── 业务便捷方法 ──
    async def all(self) -> list[DoctorProfile] | None:
        now = int(time.time() * 1000)
        if self._cache and now - self._cache_at < 30_000:
            return self._cache
        if self.mode == "off":
            return None
        if self.mode == "fixture":
            try:
                payload = load_fixture(self.fixture)
            except Exception:  # noqa: BLE001
                return None
            doctors = [d for d in (self._map_doctor(it) for it in payload.get("doctors", [])) if d]
        elif self.mode == "http":
            payload = await self._fetch_http()
            if not isinstance(payload, dict):
                return None
            doctors = [d for d in (self._map_doctor(it) for it in (payload.get("data") or {}).get("list", [])) if d]
        else:
            return None
        self._cache, self._cache_at = doctors, now
        return doctors

    async def by_department(self, dept_id: str) -> list[DoctorProfile]:
        doctors = await self.all() or []
        return [d for d in doctors if d.department_id == dept_id]

    async def by_id(self, doctor_id: str) -> DoctorProfile | None:
        for d in await self.all() or []:
            if d.doctor_id == doctor_id:
                return d
        return None

    async def by_name(self, name: str) -> list[DoctorProfile]:
        key = (name or "").strip()
        if not key:
            return []
        doctors = await self.all() or []
        exact = [d for d in doctors if d.name == key]
        if exact:
            return exact
        return [d for d in doctors if key in d.name or d.name in key]

    # ── ① 真实对接位置 ──
    async def _fetch_http(self) -> dict[str, Any] | None:
        """★ 待接入：返回 None 表示未配置。实现方式同 schedule_api._fetch_http。"""
        if not self.base_url:
            return None
        return None

    # ── ② 字段映射位置 ──
    def _map_doctor(self, item: dict[str, Any]) -> DoctorProfile | None:
        """★ 待接入：HIS 字段名不同时只改这里。"""
        doctor_id = str(item.get("doctor_id") or "").strip()
        if not doctor_id:
            return None
        slots = [
            DoctorSlot(
                date=str(s.get("date") or "*"),
                period=str(s.get("period") or "上午"),  # type: ignore[arg-type]
                room=str(s.get("room") or ""),
            )
            for s in item.get("schedules") or item.get("schedule") or []
        ]
        return DoctorProfile(
            doctor_id=doctor_id,
            name=str(item.get("name") or "").strip(),
            title=str(item.get("title") or "主治医师").strip(),
            specialty=str(item.get("specialty") or "").strip(),
            department_id=str(item.get("dept_code") or item.get("department_id") or "").strip(),
            photo=None,
            schedule=slots,
        )
