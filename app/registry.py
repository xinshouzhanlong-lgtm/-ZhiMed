"""T 层：工具注册与调用管线（注册 ≠ 披露 ≠ 授权）。

三个动作物理分离：
  register()  —— 运行时持有全量工具（系统能力全集）
  disclose()  —— 决定这次能"给谁看"（见 hooks.DISCLOSURE）
  invoke()    —— 决定这次"允不允许执行"（pre_tool_use 钩子 + 参数校验）
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .hooks import disclose, hooks
from .schemas import ToolCallRecord, ToolResult


@dataclass(frozen=True)
class ToolSpec:
    """用 dataclass 而非 BaseModel：handler 与 args_model 是对象，Pydantic 校验它们无收益。"""

    name: str
    description: str
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    args_model: type | None = None


def args_fingerprint(args: dict[str, Any]) -> str:
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)
    records: list[ToolCallRecord] = field(default_factory=list)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def all_names(self) -> list[str]:
        return sorted(self._tools)

    def disclosed_for(self, channel: str) -> list[dict[str, str]]:
        allowed = disclose(channel)
        return [
            {"name": n, "description": self._tools[n].description}
            for n in sorted(self._tools)
            if n in allowed
        ]

    async def invoke(self, channel: str, name: str, args: dict[str, Any]) -> ToolResult:
        started = int(time.time() * 1000)
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(status="denied", reason="UNKNOWN_TOOL")

        # 授权：硬编码调用，不可绕过
        gate = hooks.run("pre_tool_use", {"channel": channel, "tool": name, "args": args})
        if not gate.allow:
            self._record(name, args, "denied", started, gate.reason)
            return ToolResult(status="denied", reason=gate.reason)

        if spec.args_model is not None:
            try:
                parsed = spec.args_model(**args)
                args = parsed.model_dump()
            except Exception as exc:  # noqa: BLE001 — 参数错误作为观察回喂，不抛裸异常
                self._record(name, args, "invalid_args", started, "E_INVALID_ARGS")
                return ToolResult(status="denied", reason=f"E_INVALID_ARGS: {exc}")

        try:
            result = await spec.handler(args)
        except Exception as exc:  # noqa: BLE001
            self._record(name, args, "error", started, type(exc).__name__)
            return ToolResult(status="unavailable", reason=f"{type(exc).__name__}: {exc}")

        self._record(name, args, result.status, started, result.reason)
        return result

    def _record(self, name: str, args: dict[str, Any], status: str, started: int,
                error_code: str | None) -> None:
        rec = ToolCallRecord(
            tool_name=name,
            args_hash=args_fingerprint(args),
            status=status,
            started_at=started,
            duration_ms=int(time.time() * 1000) - started,
            error_code=error_code,
        )
        self.records.append(rec)
        # 只留最近 200 条，避免内存无界
        del self.records[:-200]


registry = ToolRegistry()
