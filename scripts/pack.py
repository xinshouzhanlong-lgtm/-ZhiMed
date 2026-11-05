"""打包脚本：生成可分发的干净 zip（跨平台，剔掉虚拟环境/缓存/密钥/运行时库）。

用法：
    python scripts/pack.py                # 输出到上级目录 guide-agent-<version>-<日期>.zip
    python scripts/pack.py --out ~/Desktop/guide-agent.zip

剔除规则（任一路径片段命中即跳过）：
    venv / .venv / __pycache__ / .pytest_cache / .idea / .vscode / .git
    *.pyc *.pyo .DS_Store Thumbs.db
    data/guide.db*（运行时库）  data/.secrets.json（签名密钥，绝不外发）
    data/media/doctors/*（上传的演示图片，保留 .gitkeep 占位）
    data/*.lock  data/*.tmp
"""
from __future__ import annotations

import argparse
import fnmatch
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # guide-agent/
WORKSPACE = ROOT.parent                                 # 工作区根

VERSION = "1.0.0"

DIR_SKIP = {"venv", ".venv", "env", "__pycache__", ".pytest_cache", ".idea", ".vscode", ".git"}
FILE_SKIP = ["*.pyc", "*.pyo", ".DS_Store", "Thumbs.db", "*.lock", "*.tmp"]
PATH_SKIP = [
    "data/guide.db", "data/guide.db-shm", "data/guide.db-wal",
    "data/.secrets.json",
]

# 需要保留可执行位的文件
EXECUTABLE = {"start.sh", "stop.sh"}


def _posix(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in DIR_SKIP for p in parts[:-1]):
        return True
    if any(fnmatch.fnmatch(parts[-1], pat) for pat in FILE_SKIP):
        return True
    if rel in PATH_SKIP:
        return True
    if rel.startswith("data/media/doctors/") and not rel.endswith(".gitkeep"):
        return True
    return False


def collect() -> list[str]:
    files: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = _posix(path)
        if _skip(rel):
            continue
        files.append(rel)
    return files


def build(out: Path) -> Path:
    files = collect()

    # 设计文档一并带上，方便在 Mac 上对照改代码
    design = WORKSPACE / "designs" / "medical-guide-agent-harness-design-v5-final.md"

    staging = Path(tempfile.mkdtemp(prefix="pack-guide-agent-"))
    try:
        base = staging / "guide-agent"
        for rel in files:
            dst = base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dst)
        if design.is_file():
            (base / "docs").mkdir(exist_ok=True)
            shutil.copy2(design, base / "docs" / design.name)

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                arc = path.relative_to(staging).as_posix()
                info = zipfile.ZipInfo.from_file(path, arc)
                info.compress_type = zipfile.ZIP_DEFLATED
                # 给 .sh 打上 0755，解压后可直接 ./start.sh
                mode = 0o755 if path.name in EXECUTABLE else 0o644
                info.external_attr = (mode & 0xFFFF) << 16
                with path.open("rb") as fh:
                    zf.writestr(info, fh.read())
        return out
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 guide-agent 为可分发 zip")
    ap.add_argument("--out", default="", help="输出路径；默认上级目录 guide-agent-<version>-<日期>.zip")
    args = ap.parse_args()

    default_name = f"guide-agent-{VERSION}-{time.strftime('%Y%m%d')}.zip"
    out = Path(args.out).expanduser() if args.out else WORKSPACE / default_name

    target = build(out)
    size_mb = target.stat().st_size / 1024 / 1024

    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        leaked = [n for n in names
                  if ".secrets" in n or "guide.db" in n or "/venv/" in n or "__pycache__" in n]

    print(f"已生成: {target}")
    print(f"  文件数: {len(names)}    体积: {size_mb:.2f} MB")
    if leaked:
        print(f"  [警告] 混入了不该分发的文件: {leaked[:5]}")
        return 1
    print("  自检: 未包含 venv / 缓存 / .secrets.json / guide.db ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
