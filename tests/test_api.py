"""端到端：HTTP 入口真实驱动（不是只测函数）。"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    r = client.post("/api/auth/password", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "list_on_duty" in body["tools"]


def test_schedule_today(client):
    r = client.get("/api/schedule/today")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["rows"]) >= 3
    # fixture 模式下 api 源可用 → 不应降级
    assert body["degraded"] is False
    assert body["meta"]["source"] == "api"
    # 每行必须带来源与时间（呈现层铁律）
    for row in body["rows"]:
        assert row["source"] in {"api", "manual"}
        assert row["fetched_at"] > 0


def test_voice_dept_location(client):
    r = client.post("/api/voice/turn", json={"text": "心内科在几楼", "device_id": "t-1"})
    assert r.status_code == 200
    body = r.json()
    assert "门诊楼" in body["display_text"]
    assert body["cards"] and body["cards"][0]["kind"] == "dept"
    assert body["disclaimer"]


def test_voice_doctor_schedule(client):
    r = client.post("/api/voice/turn", json={"text": "张明医生什么时候坐诊", "device_id": "t-2"})
    body = r.json()
    assert "张明" in body["display_text"]
    assert "坐诊" in body["display_text"]


def test_voice_today_duty(client):
    r = client.post("/api/voice/turn", json={"text": "今天有哪些科室出诊", "device_id": "t-3"})
    body = r.json()
    assert "个科室出诊" in body["display_text"]
    assert len(body["cards"]) >= 3


def test_voice_redline_blocked(client):
    r = client.post("/api/voice/turn", json={"text": "我头疼吃什么药", "device_id": "t-4"})
    body = r.json()
    assert body["intent"] == "MEDICAL_REDLINE"
    assert "不能给出判断" in body["display_text"]


def test_voice_out_of_scope_guides(client):
    r = client.post("/api/voice/turn", json={"text": "今天天气怎么样", "device_id": "t-5"})
    body = r.json()
    assert body["intent"] == "OUT_OF_SCOPE"
    assert "可以帮您查" in body["display_text"]


def test_doctor_card_has_photo_after_upload(client, admin_token):
    png = io.BytesIO()
    Image.new("RGB", (700, 700), (30, 130, 210)).save(png, format="PNG")
    png.seek(0)
    r = client.post(
        "/api/doctors/doc-zhangming/photo",
        files={"file": ("a.png", png.getvalue(), "image/png")},
        data={"consent_ack": "true"},
        headers=auth(admin_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["content_hash"]

    # 出图可访问且缓存头正确
    img = client.get(body["photo"]["url_480"])
    assert img.status_code == 200
    assert img.headers["cache-control"].startswith("public")
    assert "immutable" in img.headers["cache-control"]

    # 患者端提问应带出头像
    r2 = client.post("/api/voice/turn", json={"text": "张明医生介绍", "device_id": "t-6"})
    card = r2.json()["cards"][0]
    assert card["photo_url"]


def test_photo_upload_requires_consent(client, admin_token):
    png = io.BytesIO()
    Image.new("RGB", (700, 700), (10, 10, 10)).save(png, format="PNG")
    r = client.post(
        "/api/doctors/doc-lihong/photo",
        files={"file": ("a.png", png.getvalue(), "image/png")},
        data={"consent_ack": "false"},
        headers=auth(admin_token),
    )
    assert r.status_code == 422


def test_photo_rejects_svg(client, admin_token):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = client.post(
        "/api/doctors/doc-lihong/photo",
        files={"file": ("a.png", svg, "image/png")},
        data={"consent_ack": "true"},
        headers=auth(admin_token),
    )
    assert r.status_code == 415


def test_md_propose_and_commit(client, admin_token):
    r = client.post("/api/admin/md/propose", json={
        "department_id": "dept-resp", "field": "location",
        "new_value": "门诊楼 4F 西侧", "channel": "manual",
    }, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert "门诊楼 3F 西侧" in data["patch"]["diff"]
    assert "门诊楼 4F 西侧" in data["patch"]["diff"]

    c = client.post("/api/admin/md/commit", json={
        "patch_id": data["patch"]["patch_id"], "confirm_token": data["confirm_token"],
    }, headers=auth(admin_token))
    assert c.status_code == 200, c.text
    assert c.json()["verified"] is True

    # 生效：再问一次应返回新地址
    v = client.post("/api/voice/turn", json={"text": "呼吸内科在哪", "device_id": "t-7"})
    assert "4F 西侧" in v.json()["display_text"]


def test_md_commit_without_token_rejected(client, admin_token):
    r = client.post("/api/admin/md/propose", json={
        "department_id": "dept-ortho", "field": "phone", "new_value": "8899", "channel": "manual",
    }, headers=auth(admin_token))
    pid = r.json()["patch"]["patch_id"]
    c = client.post("/api/admin/md/commit", json={"patch_id": pid, "confirm_token": "bad"},
                    headers=auth(admin_token))
    assert c.status_code in (400, 403)


def test_admin_requires_auth(client):
    assert client.get("/api/admin/departments").status_code == 401
    assert client.get("/api/admin/departments", headers=auth("bad.token.here")).status_code == 401


def test_magic_link_flow(client, admin_token):
    r = client.post("/api/admin/magic-links", json={"doctor_id": "doc-wangqiang"},
                    headers=auth(admin_token))
    assert r.status_code == 200
    url = r.json()["url"]
    assert "#t=" in url
    token = url.split("#t=", 1)[1]

    e = client.post("/api/auth/exchange", json={"token": token})
    assert e.status_code == 200, e.text
    doc_token = e.json()["token"]
    assert e.json()["principal"]["role"] == "doctor"

    # 医生只能改自己
    png = io.BytesIO()
    Image.new("RGB", (700, 700), (90, 90, 200)).save(png, format="PNG")
    forbidden = client.post("/api/doctors/doc-lihong/photo",
                            files={"file": ("a.png", png.getvalue(), "image/png")},
                            headers=auth(doc_token))
    assert forbidden.status_code == 403

    ok = client.post("/api/doctors/doc-wangqiang/photo",
                     files={"file": ("a.png", png.getvalue(), "image/png")},
                     headers={**auth(doc_token), "X-Magic-Link": token})
    assert ok.status_code == 200, ok.text
    assert ok.json()["consent_source"] == "doctor_self"

    # 消费后链接失效
    assert client.post("/api/auth/exchange", json={"token": token}).status_code == 401


def test_asr_placeholder_501(client):
    assert client.get("/api/asr").status_code == 501
