"""Excel 导入的校验、确认写入、冲突与患者端集成回归。"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app import state
from app.datasource import dept_md
from app.main import app
from app.runtime import doctor_api, today_str
from app.schedule.importer import HEADERS, TEMPLATE_PATH, ImportErrorDetail, parse
from app.settings import settings

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def xlsx(rows, headers=HEADERS):
    """在真实下载模板的副本中填入测试输入；不使用另一套模板生成器。"""
    source = zipfile.ZipFile(TEMPLATE_PATH)
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as result:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                sheet = ET.fromstring(content)
                data = sheet.find(f"{{{NS}}}sheetData")
                data.clear()
                for row_num, values in enumerate([headers, *rows], 1):
                    row = ET.SubElement(data, f"{{{NS}}}row", r=str(row_num))
                    for column, value in enumerate(values):
                        if value is None:
                            continue
                        attributes = {"r": f"{chr(65 + column)}{row_num}"}
                        if isinstance(value, date):
                            cell = ET.SubElement(row, f"{{{NS}}}c", attributes | {"t": "d"})
                            ET.SubElement(cell, f"{{{NS}}}v").text = value.isoformat()
                        elif isinstance(value, tuple):
                            cell = ET.SubElement(row, f"{{{NS}}}c", attributes)
                            ET.SubElement(cell, f"{{{NS}}}f").text = value[0]
                        else:
                            cell = ET.SubElement(row, f"{{{NS}}}c", attributes | {"t": "inlineStr"})
                            inline = ET.SubElement(cell, f"{{{NS}}}is")
                            ET.SubElement(inline, f"{{{NS}}}t").text = str(value)
                dimension = sheet.find(f"{{{NS}}}dimension")
                if dimension is not None:
                    dimension.set("ref", f"A1:H{len(rows) + 1}")
                content = ET.tostring(sheet, encoding="utf-8", xml_declaration=True)
            result.writestr(name, content)
    return output.getvalue()


def row(day="2035-02-03", period="上午", room="4F-99", status="出诊"):
    return [day, "doc-zhangming", "张明", "dept-cardio", "心血管内科", period, room, status]


@pytest.fixture(autouse=True)
def clean_schedule():
    state.execute("DELETE FROM doctor_schedule")
    state.execute("DELETE FROM schedule_import")
    yield
    state.execute("DELETE FROM doctor_schedule")
    state.execute("DELETE FROM schedule_import")


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def headers(client):
    login = client.post("/api/auth/password", json={"username": "admin", "password": "admin123"})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def preview(client, headers, rows):
    return client.post("/api/admin/schedules/preview", headers=headers,
                       files={"file": ("排班.xlsx", xlsx(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def commit(client, headers, response):
    assert response.status_code == 200, response.text
    return client.post("/api/admin/schedules/commit", headers=headers,
                       json={"import_id": response.json()["import_id"]})


def test_template_download_and_empty_validation(client, headers):
    downloaded = client.get("/api/admin/schedules/template", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == TEMPLATE_PATH.read_bytes()
    assert "attachment" in downloaded.headers["content-disposition"]
    invalid = client.post("/api/admin/schedules/preview", headers=headers,
                          files={"file": ("template.xlsx", downloaded.content)})
    assert invalid.status_code == 422
    assert "没有可导入" in invalid.json()["detail"]["message"]


def test_preview_then_commit_persist_and_retry(client, headers):
    response = preview(client, headers, [row()])
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1
    assert state.schedule_rows() == []  # 预览不能改变生效排班。
    saved = commit(client, headers, response)
    assert saved.status_code == 200, saved.text
    assert state.schedule_rows()[0]["room"] == "4F-99"
    assert state.schedule_rows()[0]["updated_by"] == "admin"
    state.close()
    state.init_db()
    assert state.schedule_rows()[0]["room"] == "4F-99"
    repeated = commit(client, headers, response)
    assert repeated.json()["already_committed"] is True
    assert len(state.schedule_rows()) == 1


def test_update_one_period_preserves_other_period_and_date(client, headers):
    assert commit(client, headers, preview(client, headers, [row()])).status_code == 200
    assert commit(client, headers, preview(client, headers, [row(room="4F-88")])).status_code == 200
    effective = client.get("/api/admin/schedules?date=2035-02-03", headers=headers).json()["rows"]
    zhang = [r for r in effective if r["doctor_id"] == "doc-zhangming"]
    assert {r["period"]: r["room"] for r in zhang} == {"上午": "4F-88", "下午": "3F-12"}
    different = client.get("/api/admin/schedules?date=2035-02-04", headers=headers).json()["rows"]
    assert not any(r["source"] == "manual" for r in different)


def test_suspension_and_address_reach_patient_views(client, headers, reset_departments):
    day = today_str()
    response = preview(client, headers, [row(day=day, room="", status="停诊"),
                                         row(day=day, period="下午", room="4F-77")])
    assert commit(client, headers, response).status_code == 200
    patient = client.get("/api/doctors/doc-zhangming").json()
    assert [(r["period"], r["room"]) for r in patient["schedule"]] == [("下午", "4F-77")]
    answer = client.post("/api/voice/turn", json={"text": "张明医生什么时候坐诊", "device_id": "schedule-import"}).json()
    assert "4F-77" in answer["text"]
    assert "上午" not in answer["text"]
    proposed = client.post("/api/admin/md/propose", headers=headers,
                           json={"department_id": "dept-cardio", "field": "location",
                                 "new_value": "新门诊楼 8F", "channel": "manual"}).json()
    assert client.post("/api/admin/md/commit", headers=headers,
                       json={"patch_id": proposed["patch"]["patch_id"], "confirm_token": proposed["confirm_token"]}).status_code == 200
    home = client.get("/api/schedule/today").json()
    cardio = next(r for r in home["rows"] if r["department_id"] == "dept-cardio")
    assert cardio["source"] == "manual"
    assert cardio["location"] == "新门诊楼 8F"
    assert cardio["doctor_count"] == 4


@pytest.mark.parametrize("column,value,expected", [
    (0, "2035-02-30", "有效日期"),
    (1, "doc-unknown", "不存在"),
    (2, "别人", "姓名不一致"),
    (3, "dept-resp", "科室编号"),
    (4, "骨科", "科室名称"),
    (5, "全天", "时段"),
    (6, "", "必须填写诊室"),
    (7, "休假", "状态"),
    (6, ("1+1",), "不支持公式"),
])
def test_bad_rows_rejected_atomically(client, headers, column, value, expected):
    invalid = row()
    invalid[column] = value
    response = preview(client, headers, [row(period="下午"), invalid])
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["errors"][0]["row"] == 3
    assert expected in detail["errors"][0]["message"]
    assert state.schedule_rows() == []
    assert not state.query("SELECT * FROM schedule_import")


def test_duplicate_key_rejected(client, headers):
    response = preview(client, headers, [row(), row()])
    assert response.status_code == 422
    assert "重复" in response.json()["detail"]["errors"][0]["message"]


def test_full_department_suspension_updates_home(client, headers):
    day = today_str()
    current = client.get(f"/api/admin/schedules?date={day}", headers=headers).json()["rows"]
    stopped = [[day, r["doctor_id"], r["doctor_name"], r["department_id"], r["department_name"],
                r["period"], "", "停诊"] for r in current if r["department_id"] == "dept-cardio"]
    assert stopped
    assert commit(client, headers, preview(client, headers, stopped)).status_code == 200
    home = client.get("/api/schedule/today").json()
    cardio = next(r for r in home["rows"] if r["department_id"] == "dept-cardio")
    assert cardio["status"] == "suspended"
    assert cardio["doctor_count"] == 0
    assert cardio["doctors_preview"] == []


def test_excel_dates_and_name_only_supported(client, headers):
    values = row(day=date(2035, 2, 3))
    values[1] = values[3] = values[4] = ""
    response = preview(client, headers, [values])
    assert response.status_code == 200, response.text
    assert response.json()["rows"][0]["doctor_id"] == "doc-zhangming"
    assert response.json()["dates"] == ["2035-02-03"]


def test_concurrent_preview_conflict(client, headers):
    first = preview(client, headers, [row()])
    second = preview(client, headers, [row(room="4F-88")])
    assert commit(client, headers, first).status_code == 200
    rejected = commit(client, headers, second)
    assert rejected.status_code == 409
    assert state.schedule_rows()[0]["room"] == "4F-99"


def test_preview_expiry_and_owner(client, headers):
    response = preview(client, headers, [row()])
    import_id = response.json()["import_id"]
    state.execute("UPDATE schedule_import SET created_by='other-admin' WHERE id=?", (import_id,))
    assert commit(client, headers, response).status_code == 403
    state.execute("UPDATE schedule_import SET created_by='admin',created_at=0 WHERE id=?", (import_id,))
    assert commit(client, headers, response).status_code == 409
    assert state.schedule_rows() == []


def test_file_format_and_authentication(client, headers):
    for method, path in [("GET", "/api/admin/schedules/template"), ("GET", "/api/admin/schedules"),
                         ("POST", "/api/admin/schedules/commit")]:
        assert client.request(method, path, json={"import_id": "none"}).status_code == 401
    assert client.post("/api/admin/schedules/preview", files={"file": ("a.xlsx", b"invalid")}).status_code == 401
    for name in ("a.xls", "a.xlsx"):
        response = client.post("/api/admin/schedules/preview", headers=headers,
                               files={"file": (name, b"invalid")})
        assert response.status_code == 422
    assert client.get("/api/admin/schedules?date=invalid", headers=headers).status_code == 422


@pytest.mark.asyncio
async def test_duplicate_headers_and_row_limit():
    doctors = await doctor_api.all()
    depts = dept_md.load_all(settings.departments_md)
    with pytest.raises(ImportErrorDetail, match="表头"):
        parse(xlsx([row()], headers=[*HEADERS[:-1], "日期"]), "a.xlsx", doctors, depts)
    with pytest.raises(ImportErrorDetail, match="2000"):
        parse(xlsx([row()] * 2001), "a.xlsx", doctors, depts)
