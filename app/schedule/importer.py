"""Excel 只读解析、整批校验、预览与确认导入。"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from .. import state
from ..schemas import DeptDetail, DoctorProfile
from .view import PERIODS, day_slots

HEADERS = ("日期", "医生编号", "医生姓名", "科室编号", "科室名称", "时段", "诊室", "状态")
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
SHEET_NAME = "排班导入"
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "doctor_schedule.xlsx"


class ImportErrorDetail(Exception):
    def __init__(self, message: str, *, code: str = "E_IMPORT_INVALID", status: int = 422,
                 errors: list[dict] | None = None):
        self.message, self.code, self.status, self.errors = message, code, status, errors or []
        super().__init__(message)


def catalog_hash(doctors: list[DoctorProfile], depts: list[DeptDetail]) -> str:
    catalog = {"doctors": sorted((d.doctor_id, d.name, d.department_id) for d in doctors),
               "departments": sorted((d.department_id, d.name) for d in depts)}
    return hashlib.sha256(json.dumps(catalog, ensure_ascii=False).encode()).hexdigest()


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _date(value) -> str:
    if isinstance(value, datetime):
        if value.time().isoformat() != "00:00:00":
            raise ValueError("日期不能包含具体时间")
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(_text(value)).isoformat()
    except ValueError:
        raise ValueError("请填写有效日期，格式为 YYYY-MM-DD，或使用 Excel 日期单元格") from None


def _open(raw: bytes, filename: str):
    if len(raw) > MAX_BYTES:
        raise ImportErrorDetail("文件不能超过 5MB", status=413)
    if not filename.lower().endswith(".xlsx"):
        raise ImportErrorDetail("请上传 .xlsx 文件；旧版 .xls 请先另存为 .xlsx")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(e.file_size for e in entries) > 30 * 1024 * 1024:
                raise ImportErrorDetail("工作簿内容过大，请减少工作表和数据行")
            for entry in entries:
                if entry.filename.endswith(".bin") or "externalLinks/" in entry.filename:
                    raise ImportErrorDetail("请使用不含宏和外部链接的 .xlsx 文件")
                if entry.filename.endswith(".xml"):
                    content = archive.read(entry).upper()
                    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
                        raise ImportErrorDetail("工作簿包含不支持的 XML 内容")
        return load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    except ImportErrorDetail:
        raise
    except Exception:
        raise ImportErrorDetail("无法读取 Excel，请重新下载模板并另存为 .xlsx") from None


def parse(raw: bytes, filename: str, doctors: list[DoctorProfile], depts: list[DeptDetail]) -> list[dict]:
    book = _open(raw, filename)
    errors, rows = [], []
    by_id = {d.doctor_id: d for d in doctors}
    dept_ids = {d.department_id: d for d in depts}
    seen = set()
    try:
        if SHEET_NAME not in book.sheetnames:
            raise ImportErrorDetail(f"缺少「{SHEET_NAME}」工作表，请使用导入模板")
        sheet = book[SHEET_NAME]
        # 部分生成器不写 dimension；先计算实际范围，避免把已填写的数据当成空表。
        if sheet.max_row is None or sheet.max_column is None:
            sheet.calculate_dimension(force=True)
        if (sheet.max_row or 0) > MAX_ROWS + 1 or (sheet.max_column or 0) > 32:
            raise ImportErrorDetail(f"单次最多导入 {MAX_ROWS} 行，且不超过 32 列")
        header_cells = next(sheet.iter_rows(min_row=1, max_row=1, max_col=32))
        header_values = [_text(c.value) for c in header_cells]
        if any(header_values.count(h) != 1 for h in HEADERS):
            raise ImportErrorDetail("表头缺失或重复，请保留模板的八个字段：" + "、".join(HEADERS))
        indexes = {h: header_values.index(h) for h in HEADERS}
        for number, cells in enumerate(sheet.iter_rows(min_row=2, max_row=sheet.max_row or 1,
                                                      max_col=32), 2):
            values = {h: cells[i].value for h, i in indexes.items()}
            if not any(_text(v) for v in values.values()):
                continue
            field = "整行"
            try:
                if any(cells[i].data_type in {"f", "e"} for i in indexes.values()):
                    raise ValueError("请填写实际值，不支持公式或错误单元格")
                field = "日期"
                day = _date(values[field])
                field = "医生"
                doctor_id, name = _text(values["医生编号"]), _text(values["医生姓名"])
                if doctor_id:
                    doctor = by_id.get(doctor_id)
                    if doctor is None:
                        raise ValueError(f"医生编号「{doctor_id}」不存在")
                    if name and name != doctor.name:
                        raise ValueError("医生编号与姓名不一致")
                else:
                    matches = [d for d in doctors if d.name == name]
                    if len(matches) != 1:
                        raise ValueError("请填写已登记的医生姓名；存在同名时必须填写医生编号")
                    doctor = matches[0]
                field = "科室"
                dept = dept_ids.get(doctor.department_id)
                if dept is None:
                    raise ValueError("医生所属科室未登记，请先核对科室资料")
                if _text(values["科室编号"]) not in ("", dept.department_id):
                    raise ValueError("科室编号与医生所属科室不一致")
                if _text(values["科室名称"]) not in ("", dept.name, *dept.aliases):
                    raise ValueError("科室名称与医生所属科室不一致")
                field = "时段"
                period = _text(values[field])
                if period not in PERIODS:
                    raise ValueError("时段只能填写上午、下午或夜间")
                field = "状态"
                status = {"": "on_duty", "出诊": "on_duty", "停诊": "suspended",
                          "on_duty": "on_duty", "suspended": "suspended"}.get(_text(values[field]))
                if status is None:
                    raise ValueError("状态只能填写出诊或停诊")
                field = "诊室"
                room = _text(values[field])
                if not room and status == "on_duty":
                    raise ValueError("出诊时必须填写诊室")
                if len(room) > 100 or "\n" in room or "\r" in room:
                    raise ValueError("诊室不能超过 100 字或包含换行")
                key = (doctor.doctor_id, day, period)
                field = "整行"
                if key in seen:
                    raise ValueError("同一医生、日期、时段在文件中重复，请合并后重试")
                seen.add(key)
                rows.append({"row_number": number, "doctor_id": doctor.doctor_id,
                             "doctor_name": doctor.name, "department_id": dept.department_id,
                             "department_name": dept.name, "date": day, "period": period,
                             "room": room, "status": status})
            except ValueError as exc:
                errors.append({"row": number, "field": field, "message": str(exc)})
        if errors:
            raise ImportErrorDetail(f"发现 {len(errors)} 行错误，请修正后重新导入", errors=errors)
        if not rows:
            raise ImportErrorDetail("没有可导入的数据，请在「排班导入」表中填写排班")
        return rows
    except ImportErrorDetail:
        raise
    except Exception:
        raise ImportErrorDetail("读取工作表失败，请检查文件是否损坏") from None
    finally:
        book.close()


def preview(raw: bytes, filename: str, doctors: list[DoctorProfile], depts: list[DeptDetail], author: str) -> dict:
    rows = parse(raw, filename, doctors, depts)
    current = state.schedule_rows()
    existing = {(r["doctor_id"], r["date"], r["period"]): r for r in current}
    by_id = {d.doctor_id: d for d in doctors}
    for row in rows:
        key = (row["doctor_id"], row["date"], row["period"])
        original = day_slots(by_id[row["doctor_id"]].schedule, row["date"], include_suspended=True)
        row["action"] = ("更新已导入排班" if key in existing else
                         "覆盖原排班" if any(s.period == row["period"] for s in original) else "新增")
    import_id = uuid.uuid4().hex
    keys = {(r["doctor_id"], r["date"], r["period"]) for r in rows}
    state.save_schedule_import(import_id, Path(filename).name, rows,
                               state.schedule_fingerprint(current, keys), catalog_hash(doctors, depts), author)
    return {"import_id": import_id, "rows": rows, "count": len(rows), "expires_in_seconds": 900,
            "doctor_count": len({r["doctor_id"] for r in rows}),
            "dates": sorted({r["date"] for r in rows})}


def commit(import_id: str, doctors: list[DoctorProfile], depts: list[DeptDetail], author: str) -> dict:
    try:
        return state.commit_schedule_import(import_id, author, catalog_hash(doctors, depts))
    except ValueError as exc:
        messages = {"E_IMPORT_NOT_FOUND": (404, "导入预览不存在，请重新上传"),
                    "E_IMPORT_OWNER": (403, "只能确认本人上传的导入预览"),
                    "E_IMPORT_EXPIRED": (409, "预览已超过 15 分钟，请重新上传"),
                    "E_IMPORT_STALE": (409, "相关排班或医生资料已变更，请重新预览")}
        status, message = messages[str(exc)]
        raise ImportErrorDetail(message, code=str(exc), status=status) from None
