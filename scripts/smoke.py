"""真实 HTTP 冒烟脚本：对已启动的服务跑一遍主流程，输出可核对的证据。

用法：先 start.bat（或 uvicorn 起服务），再执行
    ..\\.venv\\Scripts\\python.exe scripts\\smoke.py
"""
from __future__ import annotations

import io
import os
import sys

import httpx

BASE = os.environ.get("GUIDE_SMOKE_BASE_URL", "http://127.0.0.1:8787")
# 每轮用独立 device_id：限流是"每终端 20 次/分"，脚本要模拟不同终端而非同一台狂刷
import time as _time
DEVICE = "smoke-" + _time.strftime("%H%M%S")
C = httpx.Client(base_url=BASE, timeout=10, trust_env=False)


def show(title: str) -> None:
    print("\n=== " + title + " ===")


def main() -> int:
    fails: list[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("  [PASS] " if cond else "  [FAIL] ") + msg)
        if not cond:
            fails.append(msg)

    show("1 healthz")
    h = C.get("/healthz").json()
    check(h["ok"], f"服务可用，工具 {len(h['tools'])} 个")

    show("2 今日排班")
    s = C.get("/api/schedule/today").json()
    check(s["ok"] and len(s["rows"]) >= 3, f"返回 {len(s['rows'])} 个科室")
    check(s["meta"]["source"] == "api", f"来源 = {s['meta']['source']}（fixture 占位实现）")
    check(len(s["conflicts"]) >= 1, f"冲突记录 {len(s['conflicts'])} 条（骨科两源位置不一致）")
    for r in s["rows"]:
        print(f"       · {r['department_name']:<8} {r['location']:<16} {r['source']:<6} {r['doctor_count']} 位医生")

    show("3 语音问答")
    cases = [
        ("心内科在几楼", "门诊楼"),
        ("今天有哪些科室出诊", "个科室出诊"),
        ("张明医生什么时候坐诊", "坐诊"),
        ("呼吸内科服务范围", "服务范围"),
        ("我头疼吃什么药", "不能给出判断"),
        ("今天天气怎么样", "可以帮您查"),
    ]
    for q, expect in cases:
        d = C.post("/api/voice/turn", json={"text": q, "device_id": DEVICE}).json()
        ok = expect in d["display_text"]
        check(ok, f"「{q}」→ {d['intent']} / {d['path']} / {d['latency']['total_ms']}ms")
        print(f"       {d['display_text'][:70]}")

    show("4 管理端：登录 → md 提案 → diff → 提交")
    login = C.post("/api/auth/password", json={"username": "admin", "password": "admin123"})
    check(login.status_code == 200, "登录成功")
    tok = login.json()["token"]
    H = {"Authorization": f"Bearer {tok}"}

    # 用带时间戳的新值，保证脚本可重复执行（否则第二次会命中 E_NO_CHANGE）
    import time as _t

    stamp = _t.strftime("%H%M%S")
    new_loc = f"门诊楼 2F 南侧 B 区 {stamp}"
    p = C.post("/api/admin/md/propose", headers=H, json={
        "department_id": "dept-ortho", "field": "location",
        "new_value": new_loc, "channel": "manual",
    })
    check(p.status_code == 200, f"提案生成（未落盘）：{p.status_code}")
    if p.status_code != 200:
        print("       " + p.text[:200])
    pd = p.json()["patch"]
    check("门诊楼 2F" in pd["diff"] and stamp in pd["diff"], "diff 同时含原值与新值")
    print("       diff 摘要：")
    for line in pd["diff"].splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            print("         " + line)

    no_token = C.post("/api/admin/md/commit", headers=H,
                      json={"patch_id": pd["patch_id"], "confirm_token": "bad"})
    check(no_token.status_code in (400, 403), f"无有效令牌被拒（{no_token.status_code}）")

    commit = C.post("/api/admin/md/commit", headers=H,
                    json={"patch_id": pd["patch_id"], "confirm_token": p.json()["confirm_token"]})
    check(commit.status_code == 200 and commit.json()["verified"], "提交成功且写后校验通过")

    v = C.post("/api/voice/turn", json={"text": "骨科在哪", "device_id": DEVICE}).json()
    check(stamp in v["display_text"], "改动立即生效（问答返回新地址）")

    show("5 头像：上传 → 出图 → 患者卡带图")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (900, 900), (28, 122, 200)).save(buf, format="PNG")
    up = C.post("/api/admin/doctors/doc-zhangming/photo", headers=H,
                files={"file": ("t.png", buf.getvalue(), "image/png")},
                data={"consent_ack": "true"})
    check(up.status_code == 200, "上传成功")
    photo = up.json()["photo"]
    img = C.get(photo["url_480"])
    check(img.status_code == 200 and img.headers["content-type"] == "image/webp", "出图为 WebP")
    check("immutable" in img.headers.get("cache-control", ""), "缓存头 immutable")
    print(f"       480 图 {len(img.content)} 字节；192 缩略图 {photo['url_192']}")

    q = C.post("/api/voice/turn", json={"text": "张明医生介绍", "device_id": DEVICE}).json()
    check(bool(q["cards"] and q["cards"][0]["photo_url"]), "患者端医生卡带头像")

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    bad = C.post("/api/admin/doctors/doc-lihong/photo", headers=H,
                 files={"file": ("a.png", svg, "image/png")}, data={"consent_ack": "true"})
    check(bad.status_code == 415, f"SVG 被拒（{bad.status_code}）")

    show("6 管理端照片维护与权限")
    doctors = C.get("/api/admin/doctors", headers=H).json()["doctors"]
    check(any(d["doctor_id"] == "doc-zhangming" for d in doctors), "管理端可查询全部医生")
    photo_path = "/api/admin/doctors/doc-zhangming/photo"
    check(C.delete(photo_path).status_code == 401, "未登录不能移除照片")
    removed = C.delete(photo_path, headers=H)
    check(removed.status_code == 200, "管理员移除照片成功")
    detail = C.get("/api/doctors/doc-zhangming").json()
    check(detail["photo"] is None, "患者端医生详情不再引用已移除照片")
    restored = C.post(photo_path, headers=H,
                      files={"file": ("t.png", buf.getvalue(), "image/png")},
                      data={"consent_ack": "true"})
    check(restored.status_code == 200, "管理员可以重新上传照片")

    show("7 静态页面")
    for path, name in (("/kiosk/", "患者端"), ("/admin/", "管理端")):
        r = C.get(path)
        check(r.status_code == 200 and "text/html" in r.headers["content-type"], f"{name} {path}")
    check(C.get("/doctor/").status_code == 404, "医生端入口已移除")
    check(C.post("/api/auth/exchange", json={"token": "retired"}).status_code == 404,
          "医生直链换票接口已移除")

    show("结论")
    if fails:
        print(f"  {len(fails)} 项失败：")
        for f in fails:
            print("   - " + f)
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
