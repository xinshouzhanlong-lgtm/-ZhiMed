"""公共自助机结束咨询后的上下文隔离与在途请求回归。"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app import loop, state
from app.main import app
from app.schemas import TurnContext, TurnInput


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def identifiers():
    suffix = uuid.uuid4().hex
    return "privacy-" + suffix, "device-" + suffix


def ask(client, session_id, device_id, text):
    response = client.post("/api/voice/turn", json={"session_id": session_id,
                           "device_id": device_id, "text": text})
    assert response.status_code == 200
    return response.json()


def test_end_clears_context_preserves_logs_and_cannot_resume(client):
    session_id, device_id = identifiers()
    ask(client, session_id, device_id, "心内科在哪里")
    assert state.load_session(session_id)["last_dept_id"] == "dept-cardio"
    logs_before = state.query("SELECT * FROM query_log WHERE session_id=?", (session_id,))
    assert logs_before
    response = client.post("/api/voice/session/end", json={"session_id": session_id, "device_id": device_id})
    assert response.status_code == 200
    assert state.load_session(session_id) is None
    ended = state.one("SELECT * FROM session WHERE id=?", (session_id,))
    assert ended["expires_at"] == 0
    assert ended["last_dept_id"] == ended["last_doctor_id"] == ended["last_intent"] == ""
    assert not state.save_session(session_id, device_id, "ASK_DEPT_CONTACT", "dept-cardio", None)
    assert "咨询已结束" in ask(client, session_id, device_id, "联系电话呢")["text"]
    assert len(state.query("SELECT * FROM query_log WHERE session_id=?", (session_id,))) == len(logs_before)


def test_new_patient_has_no_previous_context_or_loop_guard(client):
    session_id, device_id = identifiers()
    ask(client, session_id, device_id, "心内科在哪里")
    assert "8801" in ask(client, session_id, device_id, "联系电话呢")["text"]
    for _ in range(3):
        repeated = ask(client, session_id, device_id, "心内科在哪里")
    assert repeated["path"] == "fallback"
    client.post("/api/voice/session/end", json={"session_id": session_id, "device_id": device_id})
    new_session = uuid.uuid4().hex
    assert "8801" not in ask(client, new_session, device_id, "联系电话呢")["text"]
    answer = ask(client, new_session, device_id, "心内科在哪里")
    assert answer["path"] == "fast"
    assert "心血管内科" in answer["text"]


def test_another_device_cannot_read_or_end_session(client):
    session_id, device_id = identifiers()
    ask(client, session_id, device_id, "心内科在哪里")
    other = device_id + "-other"
    assert state.load_session(session_id, other) is None
    denied = client.post("/api/voice/session/end", json={"session_id": session_id, "device_id": other})
    assert denied.status_code == 404
    assert state.load_session(session_id, device_id)["last_dept_id"] == "dept-cardio"
    assert "8801" not in ask(client, session_id, other, "联系电话呢")["text"]


def test_end_before_first_turn_and_repeat_end(client):
    session_id, device_id = identifiers()
    payload = {"session_id": session_id, "device_id": device_id}
    assert client.post("/api/voice/session/end", json=payload).status_code == 200
    assert client.post("/api/voice/session/end", json=payload).status_code == 200
    assert "咨询已结束" in ask(client, session_id, device_id, "心内科在哪里")["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["classify", "execute"])
async def test_inflight_request_cannot_restore_closed_context(monkeypatch, stage):
    session_id, device_id = identifiers()
    started, release = asyncio.Event(), asyncio.Event()
    target = loop.intent_mod if stage == "classify" else loop
    name = "classify" if stage == "classify" else "_execute"
    original = getattr(target, name)

    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(target, name, delayed)
    ctx = TurnContext(device_id=device_id, now=state.now_ms(),
                      budget={"deadline_ms": state.now_ms() + 6000})
    task = asyncio.create_task(loop.handle_turn(
        TurnInput(session_id=session_id, device_id=device_id, text="心内科在哪里"), ctx))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert loop.end_conversation(session_id, device_id)
    release.set()
    result = await task
    assert "咨询已结束" in result.text
    assert "心血管内科" not in result.text
    assert state.load_session(session_id) is None
    assert (device_id, session_id) not in loop._TURN_CACHE


def test_reset_does_not_bypass_device_rate_limit(client):
    _, device_id = identifiers()
    for _ in range(20):
        session_id = uuid.uuid4().hex
        ask(client, session_id, device_id, "今天有哪些科室出诊")
        client.post("/api/voice/session/end", json={"session_id": session_id, "device_id": device_id})
    assert "请求过于频繁" in ask(client, uuid.uuid4().hex, device_id, "心内科在哪里")["text"]
