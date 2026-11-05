"""S 层：SQLite 状态存储（单写者 + WAL）。

纪律（对应设计文档 §4.4）：
- md 文件是内容真相源，DB 只存快照、索引与审计；
- 唯一写入口是 md/patch 与本模块的仓储函数，业务代码不要直接执行 SQL；
- 外部副作用（写 md / 写头像）都要"重读校验"，不信任写入返回值。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .settings import settings

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
  id TEXT PRIMARY KEY, device_id TEXT, created_at INTEGER, expires_at INTEGER,
  last_intent TEXT, last_dept_id TEXT, last_doctor_id TEXT
);
CREATE TABLE IF NOT EXISTS message (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS query_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, raw_text TEXT, intent TEXT,
  slots_json TEXT, path TEXT, latency_json TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS answer_record (
  id TEXT PRIMARY KEY, intent TEXT, answer_text TEXT, tool_calls_json TEXT,
  grounded_fields_json TEXT, grounding_pass INTEGER, source_version TEXT,
  model_version TEXT, prompt_version TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS md_version (
  id TEXT PRIMARY KEY, file TEXT, content_hash TEXT, content TEXT, author TEXT,
  channel TEXT, asr_confidence REAL, confirmed_by TEXT, raw_utterance TEXT,
  created_at INTEGER, verified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS md_patch (
  patch_id TEXT PRIMARY KEY, department_id TEXT, field TEXT, before_value TEXT,
  after_value TEXT, diff TEXT, base_hash TEXT, after_hash TEXT,
  asr_confidence REAL, raw_utterance TEXT, status TEXT, created_at INTEGER, committed_at INTEGER
);
CREATE TABLE IF NOT EXISTS source_health (
  source_id TEXT, checked_at INTEGER, ok INTEGER, latency_ms INTEGER, error_code TEXT,
  PRIMARY KEY (source_id, checked_at)
);
CREATE TABLE IF NOT EXISTS data_conflict (
  id TEXT PRIMARY KEY, date TEXT, department_id TEXT, api_value TEXT, manual_value TEXT,
  winner TEXT, resolved_by TEXT, resolved_at INTEGER, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS badcase (
  id INTEGER PRIMARY KEY AUTOINCREMENT, query_text TEXT, reason TEXT, expected TEXT,
  actual TEXT, created_at INTEGER, fixed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS doctor_photo (
  doctor_id TEXT, content_hash TEXT, ext TEXT, width INTEGER, height INTEGER, bytes INTEGER,
  source_sha256 TEXT, uploaded_by TEXT, uploader_role TEXT, consent_source TEXT,
  uploaded_at INTEGER, status TEXT, PRIMARY KEY (doctor_id, content_hash)
);
CREATE TABLE IF NOT EXISTS photo_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, doctor_id TEXT, action TEXT, content_hash TEXT,
  actor TEXT, actor_role TEXT, consent_source TEXT, ip_hash TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS staff (
  employee_no TEXT PRIMARY KEY, role TEXT, doctor_id TEXT, dept_scope_json TEXT,
  display_name TEXT, active INTEGER DEFAULT 1, password_hash TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS magic_link (
  jti TEXT PRIMARY KEY, doctor_id TEXT, issued_by TEXT, created_at INTEGER, expires_at INTEGER,
  consumed_at INTEGER, revoked_at INTEGER, first_ip_hash TEXT
);
CREATE TABLE IF NOT EXISTS auth_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, principal_sub TEXT, entry TEXT,
  ip_hash TEXT, ua_hash TEXT, created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_message_session ON message(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_answer_intent ON answer_record(intent, created_at);
CREATE INDEX IF NOT EXISTS idx_photo_doctor_status ON doctor_photo(doctor_id, status);
CREATE INDEX IF NOT EXISTS idx_conflict_date ON data_conflict(date);
CREATE INDEX IF NOT EXISTS idx_magic_link_doctor ON magic_link(doctor_id, expires_at);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        settings.db_file.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(str(settings.db_file), check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA foreign_keys=ON")
        _CONN.execute("PRAGMA busy_timeout=5000")
    return _CONN


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """单写者事务：全局锁 + 提交/回滚。"""
    with _LOCK:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def init_db() -> None:
    with _LOCK:
        c = conn()
        c.executescript(SCHEMA)
        c.commit()


def query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    with _LOCK:
        return list(conn().execute(sql, params).fetchall())


def one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | list = ()) -> None:
    with tx() as c:
        c.execute(sql, params)


# ─────────────────────────── 仓储函数 ───────────────────────────


def log_query(session_id: str | None, raw: str, intent: str, slots: dict, path: str, latency: dict) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO query_log(session_id, raw_text, intent, slots_json, path, latency_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (session_id or "", raw, intent, json.dumps(slots, ensure_ascii=False), path,
             json.dumps(latency, ensure_ascii=False), now_ms()),
        )
        return int(cur.lastrowid or 0)


def save_answer(answer_id: str, intent: str, text: str, tool_calls: list, grounded: list,
                grounding_pass: bool, source_version: str) -> None:
    execute(
        "INSERT OR REPLACE INTO answer_record(id, intent, answer_text, tool_calls_json,"
        " grounded_fields_json, grounding_pass, source_version, model_version, prompt_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (answer_id, intent, text, json.dumps(tool_calls, ensure_ascii=False),
         json.dumps(grounded, ensure_ascii=False), 1 if grounding_pass else 0,
         source_version, "rule+template", "v1", now_ms()),
    )


def record_conflict(date: str, dept_id: str, api_value: str, manual_value: str, winner: str) -> str:
    cid = f"{date}:{dept_id}"
    execute(
        "INSERT OR REPLACE INTO data_conflict(id, date, department_id, api_value, manual_value,"
        " winner, resolved_by, resolved_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, date, dept_id, api_value, manual_value, winner, "", 0, now_ms()),
    )
    return cid


def list_conflicts(limit: int = 50) -> list[dict]:
    rows = query(
        "SELECT * FROM data_conflict WHERE resolved_at = 0 ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def record_source_health(source_id: str, ok: bool, latency_ms: int, error_code: str | None) -> None:
    execute(
        "INSERT OR REPLACE INTO source_health(source_id, checked_at, ok, latency_ms, error_code)"
        " VALUES (?,?,?,?,?)",
        (source_id, now_ms(), 1 if ok else 0, latency_ms, error_code or ""),
    )


def latest_source_health() -> list[dict]:
    rows = query(
        "SELECT source_id, MAX(checked_at) AS ts FROM source_health GROUP BY source_id"
    )
    out: list[dict] = []
    for r in rows:
        detail = one("SELECT * FROM source_health WHERE source_id=? AND checked_at=?", (r["source_id"], r["ts"]))
        if detail:
            out.append(dict(detail))
    return out


def add_badcase(query_text: str, reason: str, expected: str, actual: str) -> None:
    execute(
        "INSERT INTO badcase(query_text, reason, expected, actual, created_at) VALUES (?,?,?,?,?)",
        (query_text, reason, expected, actual, now_ms()),
    )


def save_session(session_id: str, device_id: str, last_intent: str,
                 last_dept_id: str | None, last_doctor_id: str | None) -> None:
    execute(
        "INSERT OR REPLACE INTO session(id, device_id, created_at, expires_at, last_intent,"
        " last_dept_id, last_doctor_id) VALUES (?,?,?,?,?,?,?)",
        (session_id, device_id, now_ms(), now_ms() + 30 * 60 * 1000, last_intent,
         last_dept_id or "", last_doctor_id or ""),
    )


def load_session(session_id: str) -> dict | None:
    row = one("SELECT * FROM session WHERE id=? AND expires_at > ?", (session_id, now_ms()))
    return dict(row) if row else None


# ─────────────────────────── 头像 ───────────────────────────


def upsert_photo(rec: dict) -> None:
    with tx() as c:
        c.execute("UPDATE doctor_photo SET status='superseded' WHERE doctor_id=? AND status='active'",
                  (rec["doctor_id"],))
        c.execute(
            "INSERT OR REPLACE INTO doctor_photo(doctor_id, content_hash, ext, width, height, bytes,"
            " source_sha256, uploaded_by, uploader_role, consent_source, uploaded_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["doctor_id"], rec["content_hash"], rec.get("ext", "webp"), rec["width"],
             rec["height"], rec["bytes"], rec.get("source_sha256", ""), rec.get("uploaded_by", ""),
             rec.get("uploader_role", ""), rec.get("consent_source", ""), rec["uploaded_at"], "active"),
        )
        c.execute(
            "INSERT INTO photo_audit(doctor_id, action, content_hash, actor, actor_role,"
            " consent_source, ip_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (rec["doctor_id"], "upload", rec["content_hash"], rec.get("uploaded_by", ""),
             rec.get("uploader_role", ""), rec.get("consent_source", ""), rec.get("ip_hash", ""),
             now_ms()),
        )


def active_photo(doctor_id: str) -> dict | None:
    row = one(
        "SELECT * FROM doctor_photo WHERE doctor_id=? AND status='active'"
        " ORDER BY uploaded_at DESC LIMIT 1",
        (doctor_id,),
    )
    return dict(row) if row else None


def suspend_photo(doctor_id: str) -> None:
    execute("UPDATE doctor_photo SET status='suspended' WHERE doctor_id=? AND status='active'", (doctor_id,))


# ─────────────────────────── 认证 ───────────────────────────


def staff_by_id(employee_no: str) -> dict | None:
    row = one("SELECT * FROM staff WHERE employee_no=? AND active=1", (employee_no,))
    return dict(row) if row else None


def upsert_staff(employee_no: str, role: str, doctor_id: str | None, dept_scope: list[str],
                 display_name: str, password_hash: str | None = None) -> None:
    execute(
        "INSERT OR REPLACE INTO staff(employee_no, role, doctor_id, dept_scope_json, display_name,"
        " active, password_hash, created_at) VALUES (?,?,?,?,?,1,?,?)",
        (employee_no, role, doctor_id or "", json.dumps(dept_scope, ensure_ascii=False),
         display_name, password_hash or "", now_ms()),
    )


def create_magic_link(jti: str, doctor_id: str, issued_by: str, expires_at: int) -> None:
    execute(
        "INSERT INTO magic_link(jti, doctor_id, issued_by, created_at, expires_at, consumed_at,"
        " revoked_at, first_ip_hash) VALUES (?,?,?,?,?,0,0,'')",
        (jti, doctor_id, issued_by, now_ms(), expires_at),
    )


def magic_link_row(jti: str) -> dict | None:
    row = one("SELECT * FROM magic_link WHERE jti=?", (jti,))
    return dict(row) if row else None


def consume_magic_link(jti: str) -> None:
    execute("UPDATE magic_link SET consumed_at=? WHERE jti=?", (now_ms(), jti))


def revoke_magic_links(doctor_id: str) -> int:
    with tx() as c:
        cur = c.execute(
            "UPDATE magic_link SET revoked_at=? WHERE doctor_id=? AND revoked_at=0",
            (now_ms(), doctor_id),
        )
        return cur.rowcount


def audit(event: str, sub: str = "", entry: str = "", ip_hash: str = "", ua_hash: str = "") -> None:
    execute(
        "INSERT INTO auth_audit(event, principal_sub, entry, ip_hash, ua_hash, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (event, sub, entry, ip_hash, ua_hash, now_ms()),
    )


def login_fail_count(account: str, window_ms: int = 5 * 60 * 1000) -> int:
    row = one(
        "SELECT COUNT(*) AS n FROM auth_audit WHERE event='login_fail' AND principal_sub=?"
        " AND created_at > ?",
        (account, now_ms() - window_ms),
    )
    return int(row["n"]) if row else 0


def seed_admin(username: str, password_hash: str) -> None:
    if staff_by_id(username) is None:
        upsert_staff(username, "admin", None, ["*"], "系统管理员", password_hash)


def close() -> None:
    global _CONN
    if _CONN is not None:
        _CONN.close()
        _CONN = None
