"""★ HIS 排班接口适配器 —— 预留实现（本期不接真实接口）。

═══════════════════════════════════════════════════════════════════
真实对接时，**只需要改本文件的两个函数**，其余全部不用动：
    ① `_fetch_http()`  把 HTTP 请求填进去（地址/鉴权/超时）
    ② `_map_row()`     把 HIS 字段映射成我们的 ScheduleRow
其余部分（四态策略、熔断、降级、冲突记录、UI 呈现）已经写好并测过。
═══════════════════════════════════════════════════════════════════

预期数据契约（HIS → 本系统）
─────────────────────────────────────────────────────────────────
  请求：GET {base_url}?date=YYYY-MM-DD
        鉴权：由 `auth_env` 指定的环境变量注入（如 Header: X-App-Secret）
  响应（本系统约定的最小集合，字段名可与 HIS 不同，靠 `_map_row` 转换）：
  {
    "code": 0,                       # 0 = 成功，非 0 视为业务失败
    "msg": "ok",
    "data": { "list": [              # 当天出诊科室清单
      {
        "dept_code":  "dept-cardio", # → department_id（必须与 departments.md 的 id 对齐）
        "dept_name":  "心血管内科",   # → department_name
        "campus":     "本部",        # → campus_id
        "floor_room": "门诊楼 3F 东侧",# → location
        "doctor_cnt": 4,             # → doctor_count
        "state":      "on_duty"      # on_duty / suspended
      }
    ]}
  }
  错误码映射：HTTP 非 2xx → HTTP_5XX；超时 → TIMEOUT；JSON 解析失败 → PARSE_ERROR；
              未配置任何真实地址 → NOT_CONFIGURED（此时四态策略会按规则降级）。
"""
from __future__ import annotations

import time
from typing import Any

from ..schemas import ScheduleRow, SourceQuery, SourceResult
from ..settings import settings
from .base import load_fixture

# HIS 字段 → 内部字段。字段名一变只改这张表。
FIELD_MAP = {
    "dept_code": "department_id",
    "dept_name": "department_name",
    "campus": "campus_id",
    "floor_room": "location",
    "doctor_cnt": "doctor_count",
    "state": "status",
}


class ScheduleApiAdapter:
    """排班接口适配器。mode: off | fixture | http。"""

    id = "schedule-api"
    kind = "schedule"

    def __init__(self) -> None:
        cfg = settings.endpoint("schedule_api")
        self.mode: str = cfg.get("mode", "off")
        self.fixture: str = cfg.get("fixture", "data/fixtures/schedule_api.json")
        self.simulate: str = cfg.get("simulate", "ok")
        self.base_url: str = cfg.get("base_url", "")
        self.timeout_ms: int = int(cfg.get("timeout_ms", 2000))
        self.auth_env: str = cfg.get("auth_env", "GUIDE_HIS_APP_SECRET")

    # ── 对外契约 ──
    async def health(self) -> dict[str, Any]:
        return {"id": self.id, "mode": self.mode, "simulate": self.simulate,
                "configured": bool(self.base_url), "ok": self.mode != "off"}

    async def fetch(self, q: SourceQuery) -> SourceResult:
        now = int(time.time() * 1000)

        if self.mode == "off":
            # 模拟"HIS 不可用"：用于演示/测试四态降级，不抛异常
            return SourceResult(ok=False, fetched_at=now, error_code="NOT_CONFIGURED")

        if self.mode == "fixture":
            if self.simulate == "timeout":
                return SourceResult(ok=False, fetched_at=now, error_code="TIMEOUT")
            if self.simulate == "500":
                return SourceResult(ok=False, fetched_at=now, error_code="HTTP_5XX")
            try:
                payload = load_fixture(self.fixture)
            except FileNotFoundError:
                return SourceResult(ok=False, fetched_at=now, error_code="NOT_CONFIGURED")
            except Exception:  # noqa: BLE001
                return SourceResult(ok=False, fetched_at=now, error_code="PARSE_ERROR")
            return self._to_result(payload, now)

        if self.mode == "http":
            payload = await self._fetch_http(q)
            if payload is None:
                return SourceResult(ok=False, fetched_at=now, error_code="NOT_CONFIGURED")
            if isinstance(payload, str):  # 错误码字符串
                return SourceResult(ok=False, fetched_at=now, error_code=payload)  # type: ignore[arg-type]
            return self._to_result(payload, now)

        return SourceResult(ok=False, fetched_at=now, error_code="NOT_CONFIGURED")

    # ── ① 真实对接位置 ──
    async def _fetch_http(self, q: SourceQuery) -> dict[str, Any] | str | None:
        """★ 待接入：返回 None 表示未配置；返回 str 表示错误码；返回 dict 表示原始响应。

        实现示例（依赖 httpx，已在本期依赖中）：
            import httpx
            if not self.base_url:
                return None
            headers = {}
            secret = settings.his_app_secret
            if secret:
                headers["X-App-Secret"] = secret
            try:
                async with httpx.AsyncClient(timeout=self.timeout_ms / 1000) as client:
                    resp = await client.get(self.base_url, params={"date": q.date}, headers=headers)
            except httpx.TimeoutException:
                return "TIMEOUT"
            except httpx.HTTPError:
                return "HTTP_5XX"
            if resp.status_code >= 400:
                return "HTTP_5XX"
            try:
                return resp.json()
            except ValueError:
                return "PARSE_ERROR"
        """
        if not self.base_url:
            return None
        # 本期不发起真实请求：显式返回 None，让策略层走降级，避免"假装成功"
        return None

    # ── ② 字段映射位置 ──
    def _map_row(self, item: dict[str, Any], now: int) -> ScheduleRow | None:
        """★ 待接入：HIS 字段名不同时只改这里。"""
        get = lambda k: item.get(k)  # noqa: E731
        dept_id = str(get("dept_code") or get("department_id") or "").strip()
        if not dept_id:
            return None
        state = str(get("state") or get("status") or "on_duty").strip()
        try:
            cnt = int(get("doctor_cnt") or get("doctor_count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        return ScheduleRow(
            department_id=dept_id,
            department_name=str(get("dept_name") or get("department_name") or "").strip(),
            campus_id=str(get("campus") or get("campus_id") or "本部").strip(),
            location=str(get("floor_room") or get("location") or "").strip(),
            doctor_count=cnt,
            status="suspended" if state in {"suspended", "off", "停诊"} else "on_duty",
            source="api",
            fetched_at=now,
            stale=False,
        )

    def _to_result(self, payload: dict[str, Any], now: int) -> SourceResult:
        if int(payload.get("code", 0)) != 0:
            return SourceResult(ok=False, fetched_at=now, error_code="HTTP_5XX")
        items = (payload.get("data") or {}).get("list") or []
        rows = [r for r in (self._map_row(it, now) for it in items) if r is not None]
        if not rows:
            return SourceResult(ok=False, fetched_at=now, error_code="PARSE_ERROR")
        return SourceResult(ok=True, rows=rows, fetched_at=now)
