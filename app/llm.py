"""LLM 调用（槽位抽取 / 句式润色）。

本期默认**关闭**（config/app.json: llm.enabled=false）：
- 关闭时 `complete_json()` 返回 None，意图识别自动退回规则抽取，功能不缺失；
- 打开后只做两件事，**不生成事实**：① 把自然语言抽成 {"intent","slots"}；② 对模板答案做句式润色（润色后仍要过 grounding）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .settings import settings

try:  # httpx 是本期依赖；缺失也不应让主流程崩
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


def enabled() -> bool:
    return bool(settings.llm.get("enabled")) and httpx is not None


async def complete_json(system: str, user: str) -> dict[str, Any] | None:
    """调用 OpenAI 兼容接口，要求返回 JSON。失败/关闭一律返回 None（由调用方降级）。"""
    if not enabled():
        return None
    cfg = settings.llm
    url = cfg.get("base_url", "").rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.get("model", "qwen2.5:7b"),
        "temperature": 0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    timeout = float(cfg.get("timeout_ms", 4000)) / 1000
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                return None
            content = resp.json()["choices"][0]["message"]["content"]
            return _loads(content)
    except Exception:  # noqa: BLE001 — 任何失败都降级，不向上抛
        return None


def _loads(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def polish(template_text: str, facts: dict[str, Any]) -> str | None:
    """句式润色。**不得新增事实**，故只允许改写语序，不允许增删数字/地点/职称。"""
    if not enabled():
        return None
    system = (
        "你是医院导诊话术润色器。要求：只调整语序与语气使其更口语，"
        "严禁新增、删除或修改任何事实（地点、楼层、诊室号、职称、时间、电话）。"
        "只输出润色后的句子，不要解释。"
    )
    out = await complete_json(system, template_text)
    if not out:
        return None
    text = str(out.get("text") or out.get("result") or "").strip()
    return text or None
