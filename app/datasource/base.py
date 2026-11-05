"""T 层外部数据源的抽象层（★ HIS 对接预留位置）。

设计要点（对应设计文档 §7.1）：
- `DataSourceAdapter` 是**唯一**的外部数据契约：业务代码只认它，不认具体实现；
- 三种 mode：`off`（模拟不可用）/ `fixture`（本地快照占位）/ `http`（真实对接，未实现）；
- 熔断器与"数据来源四态"的判定在 policy 层，不在适配器里——适配器只负责"取数成功/失败"。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..schemas import SourceQuery, SourceResult
from ..settings import ROOT

CircuitState = str  # "closed" | "open" | "half_open"


@dataclass
class CircuitBreaker:
    """连续失败 N 次打开熔断，持续 open_ms 后允许半开试探。"""

    name: str
    failure_threshold: int = 3
    open_ms: int = 60_000
    _failures: int = 0
    _opened_at: int = 0
    _state: str = "closed"

    @property
    def state(self) -> str:
        if self._state == "open" and _now_ms() - self._opened_at >= self.open_ms:
            self._state = "half_open"
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = _now_ms()

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = 0
        self._state = "closed"

    def snapshot(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "failures": self._failures}


def _now_ms() -> int:
    return int(time.time() * 1000)


@runtime_checkable
class DataSourceAdapter(Protocol):
    """所有外部数据源的统一契约。新增一个源 = 新增一个实现，不改 policy / loop。"""

    id: str
    kind: str  # "schedule" | "department" | "doctor"

    async def health(self) -> dict[str, Any]: ...

    async def fetch(self, q: SourceQuery) -> SourceResult: ...


@dataclass
class AdapterRegistry:
    adapters: dict[str, DataSourceAdapter] = field(default_factory=dict)

    def register(self, adapter: DataSourceAdapter) -> None:
        self.adapters[adapter.id] = adapter

    def get(self, adapter_id: str) -> DataSourceAdapter:
        if adapter_id not in self.adapters:
            raise KeyError(f"数据源未注册: {adapter_id}")
        return self.adapters[adapter_id]

    def list_ids(self) -> list[str]:
        return sorted(self.adapters)


# ── 占位实现共用：读本地快照 ──


def load_fixture(rel_path: str) -> dict[str, Any]:
    path = ROOT / rel_path
    if not path.is_file():
        raise FileNotFoundError(f"fixture 不存在: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def fixture_path(rel_path: str) -> Path:
    return ROOT / rel_path
