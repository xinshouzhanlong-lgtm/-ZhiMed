"""端到端：HTTP 入口真实驱动（不是只测函数）。"""
from __future__ import annotations

import io
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app import state
from app.settings import settings


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
        "/api/admin/doctors/doc-zhangming/photo",
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
        "/api/admin/doctors/doc-lihong/photo",
        files={"file": ("a.png", png.getvalue(), "image/png")},
        data={"consent_ack": "false"},
        headers=auth(admin_token),
    )
    assert r.status_code == 422


def test_photo_rejects_svg(client, admin_token):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = client.post(
        "/api/admin/doctors/doc-lihong/photo",
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


def test_admin_photo_replace_and_remove(client, admin_token):
    doctor_id = "doc-wangqiang"
    path = f"/api/admin/doctors/{doctor_id}/photo"
    png = io.BytesIO()
    Image.new("RGB", (700, 700), (90, 90, 200)).save(png, format="PNG")
    first = client.post(path, files={"file": ("a.png", png.getvalue(), "image/png")},
                        data={"consent_ack": "true"}, headers=auth(admin_token))
    assert first.status_code == 200, first.text
    assert first.json()["consent_source"] == "admin_proxy"

    png = io.BytesIO()
    Image.new("RGB", (700, 700), (200, 90, 90)).save(png, format="PNG")
    second = client.post(path, files={"file": ("b.png", png.getvalue(), "image/png")},
                         data={"consent_ack": "true"}, headers=auth(admin_token))
    assert second.status_code == 200, second.text
    assert second.json()["content_hash"] != first.json()["content_hash"]
    detail = client.get(f"/api/doctors/{doctor_id}").json()
    assert detail["photo"]["url_480"] == second.json()["photo"]["url_480"]

    assert client.delete(path, headers=auth(admin_token)).status_code == 200
    assert client.get(f"/api/doctors/{doctor_id}").json()["photo"] is None
    card = client.post("/api/voice/turn", json={"text": "王强医生介绍", "device_id": "photo-remove"}).json()["cards"][0]
    assert card["photo_url"] is None
    audit = state.one("SELECT * FROM photo_audit WHERE doctor_id=? AND action='remove'",
                      (doctor_id,))
    assert audit and audit["actor"] == "admin" and audit["actor_role"] == "admin"
    assert client.delete(path, headers=auth(admin_token)).status_code == 200


def test_photo_maintenance_requires_admin(client):
    path = "/api/admin/doctors/doc-zhangming/photo"
    assert client.get("/api/admin/doctors").status_code == 401
    assert client.post(path, files={"file": ("a.png", b"image", "image/png")}).status_code == 401
    assert client.delete(path).status_code == 401
    legacy = jwt.encode({"sub": "doc-zhangming", "role": "doctor", "entry": "magic_link",
                         "exp": int(time.time()) + 60}, settings.jwt_secret, algorithm="HS256")
    assert client.get("/api/auth/me", headers=auth(legacy)).status_code == 401
    assert client.get("/api/admin/doctors", headers=auth(legacy)).status_code == 401
    assert client.post(path, headers=auth(legacy),
                       files={"file": ("a.png", b"image", "image/png")}).status_code == 401
    assert client.delete(path, headers=auth(legacy)).status_code == 401


def test_unknown_doctor_photo_rejected(client, admin_token):
    path = "/api/admin/doctors/doc-missing/photo"
    upload = client.post(path, headers=auth(admin_token), data={"consent_ack": "true"},
                         files={"file": ("a.png", b"image", "image/png")})
    assert upload.status_code == 404
    assert client.delete(path, headers=auth(admin_token)).status_code == 404


def test_admin_doctors_independent_of_schedule(client, admin_token, monkeypatch):
    from app.runtime import doctor_api, schedule_api

    monkeypatch.setattr(schedule_api, "mode", "off")
    response = client.get("/api/admin/doctors", headers=auth(admin_token))
    assert response.status_code == 200
    expected = {d.doctor_id for d in doctor_api._cache or []}
    assert expected
    assert {d["doctor_id"] for d in response.json()["doctors"]} == expected
    cardio = client.get("/api/admin/doctors?dept_id=dept-cardio", headers=auth(admin_token)).json()
    assert cardio["doctors"]
    assert all(d["department_id"] == "dept-cardio" for d in cardio["doctors"])


@pytest.mark.parametrize("method,path", [
    ("GET", "/doctor/"),
    ("GET", "/doctor/upload.js"),
    ("POST", "/api/auth/exchange"),
    ("POST", "/api/admin/magic-links"),
    ("POST", "/api/admin/magic-links/revoke"),
    ("POST", "/api/doctors/doc-zhangming/photo"),
    ("DELETE", "/api/doctors/doc-zhangming/photo"),
])
def test_retired_doctor_endpoints_removed(client, method, path):
    assert client.request(method, path).status_code == 404


def test_two_frontends_available(client):
    for path in ("/kiosk/", "/admin/"):
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_asr_placeholder_501(client):
    assert client.get("/api/asr").status_code == 501
