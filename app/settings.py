"""配置装配：config/*.json + 环境变量。

密钥纪律（与设计文档 §9.3 一致）：
1. jwt_secret 与 magic_link_secret 必须是两个不同密钥；
2. 只从环境变量读；缺失时开发期自动生成并落盘到 data/.secrets.json（生产必须用环境变量）；
3. 日志对 secret/token/key/password 打码。
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _ensure_secrets() -> dict[str, str]:
    """读取或生成开发期密钥。生产环境应通过 GUIDE_JWT_SECRET / GUIDE_MAGIC_LINK_SECRET 注入。"""
    store = ROOT / "data" / ".secrets.json"
    data: dict[str, str] = {}
    if store.is_file():
        try:
            data = json.loads(store.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    changed = False
    for key, env in (
        ("jwt_secret", "GUIDE_JWT_SECRET"),
        ("magic_link_secret", "GUIDE_MAGIC_LINK_SECRET"),
    ):
        val = os.environ.get(env)
        if val:
            data[key] = val
        elif not data.get(key):
            data[key] = secrets.token_urlsafe(48)
            changed = True
    if changed:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


class Settings:
    """轻量配置对象。刻意不用 BaseSettings，避免 Pydantic 对嵌套 dict 的额外约束干扰调试。"""

    def __init__(self) -> None:
        app = _read_json(ROOT / "config" / "app.json")
        sources = _read_json(ROOT / "config" / "sources.json")
        secrets_map = _ensure_secrets()

        self.app_cfg: dict[str, Any] = app
        self.sources_cfg: dict[str, Any] = sources

        self.port: int = int(os.environ.get("GUIDE_PORT") or app.get("port", 8787))
        self.public_base_url: str = os.environ.get("GUIDE_PUBLIC_BASE_URL") or app.get(
            "public_base_url", f"http://127.0.0.1:{self.port}"
        )

        self.llm: dict[str, Any] = app.get("llm", {})
        self.rag: dict[str, Any] = app.get("rag", {})
        self.asr: dict[str, Any] = app.get("asr", {})
        self.limits: dict[str, Any] = app.get("limits", {})
        self.photo_cfg: dict[str, Any] = app.get("photo", {})
        self.magic_link_ttl_hours: int = int(app.get("magic_link", {}).get("ttl_hours", 72))
        self.disclaimer: str = app.get("disclaimer", "本系统仅提供就诊指引，不提供诊断与用药建议。")

        self.jwt_secret: str = secrets_map["jwt_secret"]
        self.magic_link_secret: str = secrets_map["magic_link_secret"]
        self.jwt_ttl_minutes: int = 30

        # 路径
        self.data_dir: Path = ROOT / "data"
        self.departments_md: Path = self.data_dir / "departments.md"
        self.manual_schedule_md: Path = self.data_dir / "schedule" / "manual.md"
        self.db_file: Path = self.data_dir / "guide.db"
        self.media_dir: Path = self.data_dir / "media" / "doctors"
        self.web_dir: Path = ROOT / "web"

        # HIS 凭证：只从环境变量取，绝不落配置
        self.his_app_secret: str = os.environ.get("GUIDE_HIS_APP_SECRET", "")

    # ── 数据源策略 ──
    def policy_for(self, scope: str) -> dict[str, Any]:
        """科室级覆盖优先于全局 "*"。"""
        default = dict(self.sources_cfg.get("default", {}))
        for ov in self.sources_cfg.get("overrides", []):
            if ov.get("scope") == scope:
                default.update({k: v for k, v in ov.items() if k != "scope"})
                break
        default["scope"] = scope
        return default

    def endpoint(self, name: str) -> dict[str, Any]:
        return dict(self.sources_cfg.get("endpoints", {}).get(name, {}))

    def breaker_cfg(self) -> dict[str, Any]:
        return dict(self.sources_cfg.get("breaker", {"failure_threshold": 3, "open_ms": 60000}))


settings = Settings()
