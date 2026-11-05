"""科室地址 md 适配器：`data/departments.md` → DeptDetail。

文件结构约定（对应设计文档 §9.2）：
    ## 心血管内科
    <!-- id: dept-cardio; aliases: 心内科,心内 -->
    - 位置：门诊楼 3F 东侧
    - 诊室：3F-12 ~ 3F-18
    - 服务范围：高血压、冠心病…
    - 电话：8801

科室 id 是主键，**语音永不修改**；语音只能改四个值字段。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from ..schemas import DeptDetail

FIELD_MAP = {
    "位置": "location",
    "诊室": "rooms",
    "服务范围": "service_scope",   # 模型属性用 snake_case
    "电话": "phone",
}
# API 层字段名（camelCase）→ md 标签
LABEL_BY_FIELD = {"location": "位置", "rooms": "诊室", "serviceScope": "服务范围", "phone": "电话"}
FIELD_LABEL = LABEL_BY_FIELD

_HEAD_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
_META_RE = re.compile(r"<!--\s*id:\s*(?P<id>[^;>]+?)\s*(?:;\s*aliases:\s*(?P<aliases>[^>]*))?-->")
_FIELD_RE = re.compile(r"^-\s*(?P<label>[^：:]+?)\s*[：:]\s*(?P<value>.*)$")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse(text: str) -> list[DeptDetail]:
    """解析全部科室。返回顺序与文件中出现顺序一致。"""
    depts: list[DeptDetail] = []
    updated_at = 0
    lines = text.splitlines()

    # front-matter updated_at（仅用于展示，不参与校验）
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("updated_at:"):
                updated_at = int(time.time() * 1000)

    chash = content_hash(text)
    cur: DeptDetail | None = None
    for raw in lines:
        line = raw.rstrip()
        mh = _HEAD_RE.match(line)
        if mh:
            if cur:
                depts.append(cur)
            cur = DeptDetail(department_id="", name=mh.group("name"), content_hash=chash, updated_at=updated_at)
            continue
        if cur is None:
            continue
        mm = _META_RE.search(line)
        if mm:
            cur.department_id = mm.group("id").strip()
            if mm.group("aliases"):
                cur.aliases = [a.strip() for a in mm.group("aliases").split(",") if a.strip()]
            continue
        mf = _FIELD_RE.match(line)
        if mf:
            label = mf.group("label").strip()
            key = FIELD_MAP.get(label)
            if key:
                setattr(cur, key, mf.group("value").strip())
    if cur:
        depts.append(cur)
    return [d for d in depts if d.department_id]


def load_all(path: Path) -> list[DeptDetail]:
    return parse(read_text(path))


def find(depts: list[DeptDetail], name_or_id: str) -> DeptDetail | None:
    """按 id → 全名 → 别名 精确匹配。"""
    key = (name_or_id or "").strip()
    if not key:
        return None
    for d in depts:
        if d.department_id == key:
            return d
    for d in depts:
        if d.name == key:
            return d
    for d in depts:
        if key in d.aliases:
            return d
    return None


def search(depts: list[DeptDetail], query: str, limit: int = 5) -> list[tuple[DeptDetail, float]]:
    """轻量模糊匹配：别名/名称的最长公共子串占比打分。不引第三方分词。"""
    q = (query or "").strip()
    if not q:
        return []
    scored: list[tuple[DeptDetail, float]] = []
    for d in depts:
        best = 0.0
        for cand in [d.name, *d.aliases]:
            if not cand:
                continue
            if cand in q:
                best = max(best, 1.0 if cand == q else len(cand) / max(len(q), 1))
            else:
                common = _lcs_len(cand, q)
                best = max(best, common / max(len(cand), len(q)))
        if best >= 0.34:
            scored.append((d, round(best, 3)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（短串场景够用）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best
