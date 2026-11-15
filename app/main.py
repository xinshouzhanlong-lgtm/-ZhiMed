"""HTTP 入口（FastAPI）。

路由分组：
  /api/voice/turn            患者语音问答单轮
  /api/voice/session/end     结束患者咨询、清除上下文
  /api/schedule/today        今日排班（四态决策后）
  /api/departments/{id}      科室详情
  /api/media/doctors/{name}  头像出图（匿名可读，仅按 hash 精确匹配）
  /api/auth/*                管理员密码登录与身份查询
  /api/admin/*               管理端（md 提案/提交/回滚、医生照片、健康度）
"""
from __future__ import annotations

import time
import asyncio
from datetime import date as Date
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, RedirectResponse

from . import deps, state
from .auth import password as password_auth
from .auth.principal import encode_principal
from .core_errors import http_status_for
from .hooks import hooks, issue_token, verify_token  # noqa: F401
from .media import pipeline as media_pipeline
from .md import patch as md_patch
from .runtime import breaker, build_runtime, doctor_api, registry, schedule_api, schedule_manual
from .schemas import MdPatchSource, Principal, TurnContext, TurnInput
from .settings import ROOT, settings
from .schedule.view import day_slots
from .web import AppStaticFiles

# ─────────────────────────── 启动 ───────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.init_db()
    media_pipeline.ensure_dirs()
    build_runtime()
    md_patch.TOKEN_VERIFIER = verify_token  # 令牌由 L 层签发、md 侧只消费
    _bootstrap_admin()
    yield
    state.close()


def _bootstrap_admin() -> None:
    """首次启动创建默认管理员（admin / admin123），并打印一次性提示。

    生产部署必须立刻改密；口令不写进任何配置文件。
    """
    if state.staff_by_id("admin") is None:
        state.seed_admin("admin", password_auth.hash_password("admin123"))
        print("[bootstrap] 已创建默认管理员 admin / admin123 —— 请立即修改密码", flush=True)


app = FastAPI(title="医疗智能导诊 Agent", version="1.0.0", lifespan=lifespan)


# ─────────────────────────── 健康检查 ───────────────────────────


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "ts": int(time.time() * 1000),
        "adapters": [await a.health() for a in (schedule_api, schedule_manual)],
        "breaker": breaker.snapshot(),
        "llm_enabled": bool(settings.llm.get("enabled")),
        "tools": registry.all_names(),
    }


# ─────────────────────────── 患者端 ───────────────────────────


@app.post("/api/voice/turn")
async def voice_turn(payload: TurnInput) -> dict[str, Any]:
    from .loop import handle_turn

    ctx = TurnContext(
        device_id=payload.device_id,
        now=state.now_ms(),
        budget={"max_tool_calls": 3, "max_tokens": 3000, "deadline_ms": state.now_ms() + 6000},  # type: ignore[arg-type]
    )
    out = await handle_turn(payload, ctx)
    return out.model_dump(by_alias=True)


class SessionEnd(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)


@app.post("/api/voice/session/end")
async def end_voice_session(payload: SessionEnd) -> dict[str, bool]:
    from .loop import end_conversation

    if not end_conversation(payload.session_id, payload.device_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}


class KnowledgeQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


@app.post("/api/knowledge/ask")
async def knowledge_ask(payload: KnowledgeQuestion) -> dict[str, Any]:
    """经审核的医院说明类知识问答；不用于诊断、分诊或用药建议。"""
    from .rag.chain import answer_question

    try:
        return await answer_question(payload.question)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.get("/api/schedule/today")
async def schedule_today(date: str | None = None) -> dict[str, Any]:
    from .runtime import today_str

    day = date or today_str()
    r = await registry.invoke("patient_voice", "list_on_duty", {"date": day})
    if r.status != "ok" or r.data is None:
        return {"ok": False, "date": day, "rows": [], "reason": r.reason,
                "degraded": True, "meta": None, "conflicts": []}
    rs = r.data
    return {
        "ok": True,
        "date": day,
        "rows": [row.model_dump() for row in rs.rows],
        "degraded": rs.degraded,
        "reason": rs.reason,
        "meta": rs.meta.model_dump() if rs.meta else None,
        "conflicts": rs.conflicts,
        "disclaimer": settings.disclaimer,
    }


@app.get("/api/departments/{dept_id}")
async def department_detail(dept_id: str) -> dict[str, Any]:
    r = await registry.invoke("patient_voice", "get_department", {"name_or_id": dept_id})
    if r.status != "ok":
        raise HTTPException(status_code=404, detail="科室不存在")
    return r.data.model_dump()


@app.get("/api/doctors/{doctor_id}")
async def doctor_detail(doctor_id: str) -> dict[str, Any]:
    r = await registry.invoke("patient_voice", "get_doctor", {"name_or_id": doctor_id})
    if r.status != "ok":
        raise HTTPException(status_code=404, detail="医生不存在")
    r2 = await registry.invoke("patient_voice", "get_doctor_schedule",
                               {"doctor_id": doctor_id})
    data = r.data.model_dump()
    if r2.status == "ok":
        data["schedule"] = [s.model_dump() for s in r2.data["slots"]]
    return data


@app.get("/api/media/doctors/{filename}")
async def serve_photo(filename: str) -> FileResponse:
    path = media_pipeline.resolve_file(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="not found")
    # 文件名即内容 hash → 可 immutable
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ─────────────────────────── 认证 ───────────────────────────


@app.post("/api/auth/password")
async def login_password(payload: dict[str, str]) -> dict[str, Any]:
    try:
        p = password_auth.verify(payload.get("username", ""), payload.get("password", ""))
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(status_code=429 if code == "E_LOCKED" else 401,
                            detail="账号已锁定，请稍后再试" if code == "E_LOCKED" else "账号或密码错误") from None
    return {"token": encode_principal(p), "principal": p.model_dump()}


@app.get("/api/auth/me")
async def me(p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    return p.model_dump()


# ─────────────────────────── 管理端：md ───────────────────────────


@app.get("/api/admin/departments")
async def admin_departments(p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    return {"departments": md_patch.departments()}


@app.get("/api/admin/departments.md")
async def admin_departments_md(p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    from .datasource.dept_md import content_hash, read_text

    text = read_text(settings.departments_md)
    return {"text": text, "content_hash": content_hash(text)}


@app.post("/api/admin/md/propose")
async def md_propose(payload: dict[str, Any],
                     p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    """生成提案（不落盘）。返回 diff 与确认令牌 —— 前端必须把 diff 展示给人看。"""
    try:
        patch = md_patch.propose(
            dept_id=str(payload.get("department_id") or ""),
            field=str(payload.get("field") or ""),
            new_value=str(payload.get("new_value") or "").strip(),
            source=MdPatchSource(
                channel=str(payload.get("channel") or "manual"),  # type: ignore[arg-type]
                raw_utterance=str(payload.get("raw_utterance") or ""),
                asr_confidence=float(payload.get("asr_confidence") or 1.0),
            ),
        )
    except md_patch.MdError as exc:
        raise HTTPException(status_code=http_status_for(exc.code),
                            detail={"code": exc.code, "message": exc.message}) from None
    token = issue_token(patch.patch_id, patch.after_hash, patch.created_at)
    return {"patch": patch.model_dump(), "confirm_token": token,
            "expires_in_ms": 300_000}


@app.post("/api/admin/md/commit")
async def md_commit(payload: dict[str, Any],
                    p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    receipt = md_patch.commit(str(payload.get("patch_id") or ""),
                              str(payload.get("confirm_token") or ""), p.sub)
    if not receipt.ok:
        raise HTTPException(status_code=http_status_for(receipt.error_code or ""),
                            detail={"code": receipt.error_code, "message": receipt.message})
    return receipt.model_dump()


@app.post("/api/admin/md/rollback")
async def md_rollback(payload: dict[str, Any],
                      p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    receipt = md_patch.rollback(str(payload.get("version_id") or ""), p.sub)
    if not receipt.ok:
        raise HTTPException(status_code=400, detail=receipt.message)
    return receipt.model_dump()


@app.get("/api/admin/md/versions")
async def md_versions(p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    return {"versions": md_patch.list_versions()}


# ─────────────────────────── 管理端：医生与数据源 ───────────────────────────


@app.get("/api/admin/doctors")
async def admin_doctors(dept_id: str | None = None,
                        p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    """列出全部医生或指定科室的医生，供管理端维护照片，不依赖当天排班。"""
    from .media import photo_ref_for

    docs = await doctor_api.by_department(dept_id) if dept_id else (await doctor_api.all() or [])
    out = []
    for d in docs:
        ref = photo_ref_for(d.doctor_id)
        out.append({"doctor_id": d.doctor_id, "name": d.name, "title": d.title,
                    "department_id": d.department_id,
                    "photo": ref})
    return {"doctors": out}


@app.get("/api/admin/source-health")
async def source_health(p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    return {
        "adapters": [await a.health() for a in (schedule_api, schedule_manual)],
        "breaker": breaker.snapshot(),
        "recent": state.latest_source_health(),
        "conflicts": state.list_conflicts(),
        "policy_default": settings.policy_for("*"),
    }


# ─────────────────────────── 管理端：排班导入 ───────────────────────────


@app.get("/api/admin/schedules/template")
async def schedule_template(p: Principal = Depends(deps.get_admin)) -> FileResponse:
    from .schedule.importer import TEMPLATE_PATH

    return FileResponse(TEMPLATE_PATH, filename="医生排班导入模板.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/admin/schedules")
async def admin_schedules(date: str | None = None,
                          p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    from .datasource import dept_md
    from .runtime import today_str

    day = date or today_str()
    try:
        day = Date.fromisoformat(day).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from None
    doctors = await doctor_api.all()
    if doctors is None:
        raise HTTPException(status_code=503, detail="医生数据源暂不可用")
    depts = {d.department_id: d.name for d in dept_md.load_all(settings.departments_md)}
    rows = [{"doctor_id": d.doctor_id, "doctor_name": d.name,
             "department_id": d.department_id, "department_name": depts.get(d.department_id, d.department_id),
             **s.model_dump(), "date": day}
            for d in doctors for s in day_slots(d.schedule, day, include_suspended=True)]
    return {"date": day, "rows": rows}


@app.post("/api/admin/schedules/preview")
async def preview_schedule(file: UploadFile = File(...),
                           p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    from .datasource import dept_md
    from .schedule import importer

    doctors = await doctor_api.all()
    if doctors is None:
        raise HTTPException(status_code=503, detail="医生数据源暂不可用，请恢复后再导入")
    raw = await file.read(importer.MAX_BYTES + 1)
    depts = dept_md.load_all(settings.departments_md)
    try:
        return await asyncio.to_thread(importer.preview, raw, file.filename or "", doctors, depts, p.sub)
    except importer.ImportErrorDetail as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message,
                                                          "errors": exc.errors}) from None


class ScheduleCommit(BaseModel):
    import_id: str = Field(min_length=1, max_length=64)


@app.post("/api/admin/schedules/commit")
async def commit_schedule(payload: ScheduleCommit,
                          p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    from .datasource import dept_md
    from .schedule import importer

    doctors = await doctor_api.all()
    if doctors is None:
        raise HTTPException(status_code=503, detail="医生数据源暂不可用，请恢复后再确认")
    try:
        return importer.commit(payload.import_id, doctors,
                               dept_md.load_all(settings.departments_md), p.sub)
    except importer.ImportErrorDetail as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message}) from None


# ─────────────────────────── 管理端：头像 ───────────────────────────


@app.post("/api/admin/doctors/{doctor_id}/photo")
async def upload_photo(doctor_id: str,
                       file: UploadFile = File(...),
                       consent_ack: bool = Form(False),
                       p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    grant = deps.make_photo_grant(p, doctor_id, consent_ack)
    if await doctor_api.by_id(doctor_id) is None:
        raise HTTPException(status_code=404, detail="医生不存在")

    # 体积先拦（避免把 10MB 放进 Pillow）
    max_bytes = int(settings.photo_cfg.get("max_bytes", 10 * 1024 * 1024))
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail={"code": "E_TOO_LARGE", "message": "图片过大"})

    try:
        proc = media_pipeline.process(raw)
        rec, deduped = media_pipeline.store(proc, grant)
    except media_pipeline.MediaError as exc:
        raise HTTPException(status_code=http_status_for(exc.code),
                            detail={"code": exc.code, "message": exc.message}) from None

    return {"ok": True, "deduped": deduped,
            "photo": media_pipeline.ref_for(rec.model_dump()),
            "content_hash": rec.content_hash, "consent_source": rec.consent_source}


@app.delete("/api/admin/doctors/{doctor_id}/photo")
async def delete_photo(doctor_id: str,
                       p: Principal = Depends(deps.get_admin)) -> dict[str, Any]:
    if await doctor_api.by_id(doctor_id) is None:
        raise HTTPException(status_code=404, detail="医生不存在")
    media_pipeline.remove(doctor_id, p.sub)
    return {"ok": True}


# ─────────────────────────── 天气占位（防误引用） ───────────────────────────


@app.get("/api/asr")
async def asr_placeholder() -> dict[str, Any]:
    """服务端 ASR 预留位（本期用浏览器 Web Speech，不实现服务端转写）。"""
    raise HTTPException(status_code=501, detail="服务端 ASR 未启用，本期使用浏览器识别")


# ─────────────────────────── 静态资源 ───────────────────────────

app.mount("/kiosk", AppStaticFiles(directory=ROOT / "web" / "kiosk", html=True), name="kiosk")
app.mount("/admin", AppStaticFiles(directory=ROOT / "web" / "admin", html=True), name="admin")


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/kiosk/")


@app.get("/favicon.ico")
async def favicon() -> RedirectResponse:
    return RedirectResponse(url="/kiosk/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=False)
