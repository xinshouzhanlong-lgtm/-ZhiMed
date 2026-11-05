"""全量数据契约（对应设计文档 §8）。

约定：
- 所有出入参、内部结构都定义在此，避免"三份 Schema 各写一遍"；
- 面向"对外契约"的结构同时是 HIS 接口预留契约（见 README「预期数据契约」）。
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ─────────────────────────── 数据源 ───────────────────────────


class SourceMode(StrEnum):
    API_ONLY = "api_only"
    MANUAL_ONLY = "manual_only"
    API_THEN_MANUAL = "api_then_manual"
    MANUAL_OVERRIDE = "manual_override"


class SourcePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    scope: str
    mode: SourceMode
    timeout_ms: int = 2000
    max_stale_ms: int = 21_600_000
    reason: str | None = None
    expires_at: int | None = None


class SourceMeta(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: Literal["api", "manual"]
    fetched_at: int
    stale: bool = False
    overridden: bool = False


class SourceQuery(BaseModel):
    date: str
    scope: str | None = None
    timeout_ms: int | None = None


class SourceResult(BaseModel):
    ok: bool
    rows: list[Any] = Field(default_factory=list)
    fetched_at: int = 0
    error_code: Literal["TIMEOUT", "HTTP_5XX", "PARSE_ERROR", "NOT_CONFIGURED", "DISABLED"] | None = None


# ─────────────────────────── 领域对象 ───────────────────────────

DoctorTitle = Literal["主任医师", "副主任医师", "主治医师", "住院医师"]


class DoctorPhotoRef(BaseModel):
    url_480: str
    url_192: str
    width: int
    height: int
    bytes_480: int
    updated_at: int


class DoctorBrief(BaseModel):
    doctor_id: str
    name: str
    title: str
    photo_url: str | None = None


class ScheduleRow(BaseModel):
    department_id: str
    department_name: str
    campus_id: str = "本部"
    location: str
    doctor_count: int = 0
    doctors_preview: list[DoctorBrief] = Field(default_factory=list)
    status: Literal["on_duty", "suspended"] = "on_duty"
    source: Literal["api", "manual"] = "manual"
    fetched_at: int = 0
    stale: bool = False


class ResolvedSchedule(BaseModel):
    rows: list[ScheduleRow] = Field(default_factory=list)
    meta: SourceMeta | None = None
    degraded: bool = False
    reason: str | None = None
    conflicts: list[str] = Field(default_factory=list)


class DeptDetail(BaseModel):
    department_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    location: str = ""
    rooms: str = ""
    service_scope: str = ""
    phone: str | None = None
    content_hash: str = ""
    updated_at: int = 0


class DoctorSlot(BaseModel):
    date: str
    period: Literal["上午", "下午", "夜间"]
    room: str = ""
    quota_left: int | None = None


class DoctorProfile(BaseModel):
    doctor_id: str
    name: str
    title: str
    specialty: str
    department_id: str
    photo: DoctorPhotoRef | None = None
    schedule: list[DoctorSlot] = Field(default_factory=list)


class Candidate(BaseModel):
    id: str
    name: str
    score: float
    kind: Literal["dept", "doctor"]


# ─────────────────────────── 工具调用 ───────────────────────────


class ToolResult(BaseModel):
    status: Literal["ok", "not_found", "unavailable", "denied"]
    data: Any = None
    source: SourceMeta | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    reason: str | None = None
    degraded_to: Literal["manual"] | None = None


class ToolCallRecord(BaseModel):
    tool_name: str
    args_hash: str
    status: str
    started_at: int
    duration_ms: int
    error_code: str | None = None


class HookResult(BaseModel):
    allow: bool
    reason: str | None = None
    mutation: dict[str, Any] | None = None


# ─────────────────────────── 会话 / 问答 ───────────────────────────


class IntentKind(StrEnum):
    ASK_DEPT_LOCATION = "ASK_DEPT_LOCATION"
    ASK_DEPT_SERVICE = "ASK_DEPT_SERVICE"
    ASK_DEPT_CONTACT = "ASK_DEPT_CONTACT"
    ASK_DOCTOR_PROFILE = "ASK_DOCTOR_PROFILE"
    ASK_DOCTOR_SCHEDULE = "ASK_DOCTOR_SCHEDULE"
    ASK_TODAY_DUTY = "ASK_TODAY_DUTY"
    NAVIGATE = "NAVIGATE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    MEDICAL_REDLINE = "MEDICAL_REDLINE"


class RawEntity(BaseModel):
    type: Literal["dept", "doctor", "date"]
    text: str
    confidence: float = 1.0


class Slots(BaseModel):
    department_id: str | None = None
    doctor_id: str | None = None
    date: str | None = None
    field: str | None = None
    raw_entities: list[RawEntity] = Field(default_factory=list)


class Budget(BaseModel):
    max_tool_calls: int = 3
    max_tokens: int = 3000
    deadline_ms: int


class TurnInput(BaseModel):
    session_id: str | None = None
    text: str | None = None
    device_id: str = "unknown"


class TurnContext(BaseModel):
    channel: Literal["patient_voice"] = "patient_voice"
    device_id: str = "unknown"
    now: int
    budget: Budget


class AnswerCard(BaseModel):
    kind: Literal["dept", "doctor"]
    title: str
    subtitle: str = ""
    photo_url: str | None = None
    actions: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class LatencyBreakdown(BaseModel):
    asr_ms: int = 0
    infer_ms: int = 0
    tool_ms: int = 0
    total_ms: int = 0


class TurnOutput(BaseModel):
    answer_id: str
    intent: str = "OUT_OF_SCOPE"
    text: str
    display_text: str
    cards: list[AnswerCard] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    source_meta: SourceMeta | None = None
    degraded: bool = False
    disclaimer: str = ""
    latency: LatencyBreakdown = Field(default_factory=LatencyBreakdown)
    path: Literal["fast", "model", "fallback"] = "fallback"


class FieldCheck(BaseModel):
    field: str
    claimed: str
    actual: str
    match: bool


class GroundingReport(BaseModel):
    pass_: bool = Field(default=False, alias="pass")
    checked_fields: list[FieldCheck] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ─────────────────────────── md 修改 ───────────────────────────

FIELD_WHITELIST: frozenset[str] = frozenset({"location", "rooms", "serviceScope", "phone"})
MdField = Literal["location", "rooms", "serviceScope", "phone"]


class MdPatchSource(BaseModel):
    channel: Literal["voice", "manual"] = "voice"
    raw_utterance: str = ""
    asr_confidence: float = 1.0


class MdPatch(BaseModel):
    patch_id: str
    department_id: str
    field: str
    before_value: str
    after_value: str
    diff: str
    base_hash: str
    after_hash: str
    source: MdPatchSource = Field(default_factory=MdPatchSource)
    created_at: int = 0
    status: Literal["proposed", "committed", "rejected", "expired"] = "proposed"


class ConfirmToken(BaseModel):
    token: str
    patch_id: str
    exp: int


class CommitReceipt(BaseModel):
    ok: bool
    patch_id: str = ""
    version_id: str = ""
    content_hash: str = ""
    verified: bool = False
    error_code: str | None = None
    message: str = ""


# ─────────────────────────── 认证 ───────────────────────────

AuthEntry = Literal["magic_link", "password"]


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)
    sub: str
    role: Literal["admin", "doctor"]
    dept_scope: list[str] = Field(default_factory=list)
    display_name: str | None = None
    entry: AuthEntry = "password"
    issued_at: int = 0
    exp: int = 0


class MagicLinkClaims(BaseModel):
    model_config = ConfigDict(frozen=True)
    doctor_id: str
    jti: str
    exp: int


class MagicLinkToken(BaseModel):
    token: str
    claims: MagicLinkClaims
    url: str


# ─────────────────────────── 头像 ───────────────────────────


class PhotoWriteGrant(BaseModel):
    subject: str
    role: Literal["admin", "doctor"]
    target_doctor_id: str
    consent_source: Literal["doctor_self", "admin_proxy"]


class DoctorPhotoRecord(BaseModel):
    doctor_id: str
    content_hash: str
    ext: str = "webp"
    width: int
    height: int
    bytes: int
    source_sha256: str = ""
    uploaded_by: str = ""
    uploader_role: str = ""
    consent_source: str = ""
    uploaded_at: int = 0
    status: Literal["active", "superseded", "suspended"] = "active"


class UploadResult(BaseModel):
    ok: bool
    photo: DoctorPhotoRef | None = None
    deduped: bool = False
    error_code: str | None = None
    message: str = ""
