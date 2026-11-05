"""测试夹具：把数据/库/媒体目录重定向到临时目录，避免污染真实 data/。

必须在导入 app 其它模块前完成路径重定向（settings 是模块级单例）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TMP_ROOT = Path(tempfile.mkdtemp(prefix="guide-test-"))

from app.settings import ROOT, settings  # noqa: E402

( TMP_ROOT / "data" / "schedule" ).mkdir(parents=True, exist_ok=True)
(TMP_ROOT / "data" / "fixtures").mkdir(parents=True, exist_ok=True)
(TMP_ROOT / "data" / "media" / "doctors").mkdir(parents=True, exist_ok=True)

shutil.copy(ROOT / "data" / "departments.md", TMP_ROOT / "data" / "departments.md")
shutil.copy(ROOT / "data" / "schedule" / "manual.md", TMP_ROOT / "data" / "schedule" / "manual.md")
for name in ("schedule_api.json", "doctors.json"):
    shutil.copy(ROOT / "data" / "fixtures" / name, TMP_ROOT / "data" / "fixtures" / name)

settings.data_dir = TMP_ROOT / "data"
settings.departments_md = TMP_ROOT / "data" / "departments.md"
settings.manual_schedule_md = TMP_ROOT / "data" / "schedule" / "manual.md"
settings.db_file = TMP_ROOT / "data" / "guide.db"
settings.media_dir = TMP_ROOT / "data" / "media" / "doctors"

from app import state  # noqa: E402
from app.md import patch as md_patch  # noqa: E402
from app.hooks import verify_token  # noqa: E402

state.close()
state.init_db()
md_patch.TOKEN_VERIFIER = verify_token


@pytest.fixture(scope="session", autouse=True)
def _seed() -> None:
    from app.auth import password as password_auth

    state.seed_admin("admin", password_auth.hash_password("admin123"))


@pytest.fixture()
def reset_departments():
    """每个 md 测试前恢复一份干净副本。"""
    shutil.copy(ROOT / "data" / "departments.md", settings.departments_md)
    yield
    shutil.copy(ROOT / "data" / "departments.md", settings.departments_md)
    lock = settings.departments_md.with_suffix(".md.lock")
    if lock.exists():
        lock.unlink()


@pytest.fixture()
def reset_schedule():
    shutil.copy(ROOT / "data" / "schedule" / "manual.md", settings.manual_schedule_md)
    yield


def make_png(width: int = 600, height: int = 600, color=(30, 120, 200)) -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def write_sources(overrides: dict) -> None:
    """临时改写 config/sources.json（测试用，结束不还原会污染；仅在独立用例中使用）。"""
    path = ROOT / "config" / "sources.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(overrides)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    settings.sources_cfg = data
