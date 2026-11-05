"""md 写入通道（★ 唯一写入口，对应设计文档 §7.2）。

六道门禁全部在这里落地：
  1 置信度门（< 0.85 不提案）
  2 字段门（只允许 4 个值字段）
  3 定位门（section 必须唯一命中）
  4 乐观锁门（base_hash 必须等于当前文件 hash）
  5 确认门（commit 必须带有效 confirm_token）
  6 校验门（写后重读 hash 必须等于 after_hash，否则回滚）

文件本身是人类可维护的真相源，DB 只存快照与审计。
"""
from __future__ import annotations

import difflib
import hashlib
import os
import time
import uuid
from pathlib import Path

from .. import state
from ..datasource.dept_md import FIELD_LABEL, content_hash, parse
from ..schemas import FIELD_WHITELIST, CommitReceipt, MdPatch, MdPatchSource
from ..settings import settings

LOCK_TTL_MS = 10_000
MIN_CONFIDENCE = 0.85


class MdError(Exception):
    """带 error_code 的领域异常，由 API 层映射 HTTP 状态。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ─────────────────────────── 文件锁 ───────────────────────────


def _lock_path() -> Path:
    return settings.departments_md.with_suffix(".md.lock")


class file_lock:
    """单写者文件锁。陈旧锁（超过 TTL）可抢占。"""

    def __init__(self, path: Path | None = None, ttl_ms: int = LOCK_TTL_MS) -> None:
        self.path = path or _lock_path()
        self.ttl_ms = ttl_ms
        self.fd: int | None = None

    def __enter__(self) -> "file_lock":
        deadline = time.time() + 2.0
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(int(time.time() * 1000)).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() * 1000 - self.path.stat().st_mtime * 1000
                except FileNotFoundError:
                    continue
                if age > self.ttl_ms:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.time() > deadline:
                    raise MdError("E_LOCK_TIMEOUT", "文件被其他操作占用，请稍后重试") from None
                time.sleep(0.05)

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ─────────────────────────── 定位与改写 ───────────────────────────


def _read() -> str:
    return settings.departments_md.read_text(encoding="utf-8")


def _locate(text: str, dept_id: str) -> tuple[int, int, str, str]:
    """返回 (节起始行, 节结束行, 当前名称, 原文件中的节文本)。定位必须唯一。"""
    lines = text.splitlines(keepends=True)
    hits: list[tuple[int, int]] = []
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            section = "".join(lines[i:j])
            if f"id: {dept_id}" in section or f"id:{dept_id}" in section:
                hits.append((i, j))
    if not hits:
        raise MdError("E_DEPT_NOT_FOUND", f"未找到科室 {dept_id} 的分节")
    if len(hits) > 1:
        raise MdError("E_AMBIGUOUS_SECTION", f"科室 {dept_id} 在文件中命中 {len(hits)} 处，请人工指定")
    i, j = hits[0]
    name = lines[i].removeprefix("## ").strip()
    return i, j, name, "".join(lines[i:j])


def _current_value(section_text: str, field: str) -> str:
    label = FIELD_LABEL.get(field)
    if not label:
        raise MdError("E_FIELD_NOT_ALLOWED", f"字段 {field} 不在白名单内")
    for ln in section_text.splitlines():
        s = ln.strip()
        if s.startswith(f"- {label}：") or s.startswith(f"- {label}:"):
            return s.split("：", 1)[-1].split(":", 1)[-1].strip()
    return ""


def _replace_field(section_text: str, field: str, new_value: str) -> str:
    label = FIELD_LABEL[field]
    out: list[str] = []
    replaced = False
    for ln in section_text.splitlines():
        s = ln.strip()
        if s.startswith(f"- {label}：") or s.startswith(f"- {label}:"):
            prefix = ln[: ln.index("-")]
            out.append(f"{prefix}- {label}：{new_value}")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        raise MdError("E_FIELD_NOT_FOUND", f"该科室没有「{label}」字段，请先在 md 中添加")
    return "\n".join(out) + "\n"


def _diff(old_text: str, new_text: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="departments.md（原）",
            tofile="departments.md（改后）",
            n=2,
        )
    )


# ─────────────────────────── 提案 ───────────────────────────


def propose(dept_id: str, field: str, new_value: str, source: MdPatchSource) -> MdPatch:
    if field not in FIELD_WHITELIST:
        raise MdError("E_FIELD_NOT_ALLOWED", f"字段 {field} 不允许语音修改（仅限 位置/诊室/服务范围/电话）")
    if source.channel == "voice" and source.asr_confidence < MIN_CONFIDENCE:
        raise MdError("E_LOW_CONFIDENCE",
                      f"语音识别置信度 {source.asr_confidence:.2f} 低于 {MIN_CONFIDENCE}，请手动修正文本后重试")

    text = _read()
    base_hash = content_hash(text)
    i, j, name, section = _locate(text, dept_id)
    before = _current_value(section, field)

    new_section = _replace_field(section, field, new_value)
    lines = text.splitlines(keepends=True)
    new_text = "".join(lines[:i]) + new_section + "".join(lines[j:])
    after_hash = content_hash(new_text)
    if before == new_value:
        raise MdError("E_NO_CHANGE", "新值与当前值相同，无需修改")

    patch = MdPatch(
        patch_id=uuid.uuid4().hex,
        department_id=dept_id,
        field=field,
        before_value=before,
        after_value=new_value,
        diff=_diff(text, new_text),
        base_hash=base_hash,
        after_hash=after_hash,
        source=source,
        created_at=state.now_ms(),
        status="proposed",
    )
    state.execute(
        "INSERT INTO md_patch(patch_id, department_id, field, before_value, after_value, diff,"
        " base_hash, after_hash, asr_confidence, raw_utterance, status, created_at, committed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (patch.patch_id, dept_id, field, before, new_value, patch.diff, base_hash, after_hash,
         source.asr_confidence, source.raw_utterance, "proposed", patch.created_at),
    )
    # 保留新文本给 commit 用（不落盘，进程内即可重算）
    return patch


def _rebuild(patch: MdPatch) -> str:
    text = _read()
    i, j, _name, section = _locate(text, patch.department_id)
    new_section = _replace_field(section, patch.field, patch.after_value)
    lines = text.splitlines(keepends=True)
    return "".join(lines[:i]) + new_section + "".join(lines[j:])


def _snapshot(full_text: str, author: str, channel: str, confidence: float,
              raw: str, verified: bool) -> str:
    vid = uuid.uuid4().hex
    state.execute(
        "INSERT INTO md_version(id, file, content_hash, content, author, channel, asr_confidence,"
        " confirmed_by, raw_utterance, created_at, verified) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (vid, str(settings.departments_md), content_hash(full_text), full_text, author, channel,
         confidence, "touch", raw, state.now_ms(), 1 if verified else 0),
    )
    return vid


_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows 必须显式二进制，否则 \n 被转成 \r\n 导致 hash 不符


def _atomic_write(new_text: str) -> None:
    target = settings.departments_md
    tmp = target.with_suffix(".md.tmp")
    data = new_text.encode("utf-8")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC | _O_BINARY)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)  # 原子替换（POSIX 与 Windows 均保证原子性）


# ─────────────────────────── 提交 ───────────────────────────

# 由 hooks 注入校验函数，避免 md 侧反向依赖认证密钥实现
TOKEN_VERIFIER = None


def commit(patch_id: str, token: str, author: str) -> CommitReceipt:
    row = state.one("SELECT * FROM md_patch WHERE patch_id=?", (patch_id,))
    if not row:
        return CommitReceipt(ok=False, error_code="E_PATCH_NOT_FOUND", message="提案不存在")
    patch = MdPatch(
        patch_id=row["patch_id"], department_id=row["department_id"], field=row["field"],
        before_value=row["before_value"], after_value=row["after_value"], diff=row["diff"],
        base_hash=row["base_hash"], after_hash=row["after_hash"],
        source=MdPatchSource(channel="voice", raw_utterance=row["raw_utterance"],
                             asr_confidence=row["asr_confidence"] or 1.0),
        created_at=row["created_at"], status=row["status"],
    )
    if patch.status == "committed":
        # 幂等：重复提交直接返回首次结果
        latest = state.one("SELECT * FROM md_version WHERE file=? ORDER BY created_at DESC LIMIT 1",
                           (str(settings.departments_md),))
        return CommitReceipt(ok=True, patch_id=patch_id, version_id=(latest["id"] if latest else ""),
                             content_hash=content_hash(_read()), verified=True,
                             message="该提案已提交过，返回首次结果")

    # 门 5：确认令牌
    if TOKEN_VERIFIER is None or not TOKEN_VERIFIER(token, patch.patch_id, patch.after_hash):
        return CommitReceipt(ok=False, error_code="E_NO_TOKEN", message="确认令牌无效或已过期")

    with file_lock():
        current = _read()
        # 门 4：乐观锁
        if content_hash(current) != patch.base_hash:
            state.execute("UPDATE md_patch SET status='expired' WHERE patch_id=?", (patch_id,))
            return CommitReceipt(ok=False, error_code="E_STALE_BASE",
                                 message="文件已被他人修改，请重新生成提案")

        before_snapshot = current
        new_text = _rebuild(patch)
        if content_hash(new_text) != patch.after_hash:
            return CommitReceipt(ok=False, error_code="E_STALE_BASE",
                                 message="文件内容已变化，请重新生成提案")

        _atomic_write(new_text)

        # 门 6：写后重读校验
        reread = settings.departments_md.read_text(encoding="utf-8")
        verified = content_hash(reread) == patch.after_hash
        if not verified:
            _atomic_write(before_snapshot)
            return CommitReceipt(ok=False, error_code="E_VERIFY_FAILED",
                                 message="写入校验失败，已回滚")

        # 快照存"本次修改之前"的内容 —— 这才是回滚要回到的目标
        vid = _snapshot(before_snapshot, author, patch.source.channel,
                        patch.source.asr_confidence, patch.source.raw_utterance, verified)
        state.execute("UPDATE md_patch SET status='committed', committed_at=? WHERE patch_id=?",
                      (state.now_ms(), patch_id))
        return CommitReceipt(ok=True, patch_id=patch_id, version_id=vid,
                             content_hash=content_hash(reread), verified=True,
                             message="已保存")


def rollback(version_id: str, author: str) -> CommitReceipt:
    row = state.one("SELECT * FROM md_version WHERE id=?", (version_id,))
    if not row:
        return CommitReceipt(ok=False, error_code="E_VERSION_NOT_FOUND", message="版本不存在")
    with file_lock():
        current = _read()
        _atomic_write(row["content"])
        reread = settings.departments_md.read_text(encoding="utf-8")
        ok = content_hash(reread) == row["content_hash"]
        # 回滚前先存一份当前状态，保证回滚本身也可撤销
        _snapshot(current, author, "rollback", 1.0, f"回滚到 {version_id[:8]} 之前的状态", ok)
        if ok:
            return CommitReceipt(ok=True, patch_id="", version_id=version_id,
                                 content_hash=row["content_hash"], verified=True,
                                 message=f"已回滚到 {version_id[:8]}")
        return CommitReceipt(ok=False, error_code="E_VERIFY_FAILED", message="回滚后校验失败")


def list_versions(limit: int = 20) -> list[dict]:
    rows = state.query(
        "SELECT id, content_hash, author, channel, created_at, verified FROM md_version"
        " ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def departments() -> list[dict]:
    return [d.model_dump() for d in parse(_read())]
