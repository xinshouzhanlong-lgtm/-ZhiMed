"""页面资源更新必须绕开旧脚本缓存；头像缓存策略不受影响。"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.web import AppStaticFiles


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("page", ["kiosk", "admin"])
def test_html_and_assets_have_content_versions_and_no_store(client, page):
    response = client.get(f"/{page}/")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assets = re.findall(r'''(?:src|href)=["']([^"']+\.(?:js|css)\?v=[0-9a-f]+)["']''', response.text)
    assert len(assets) >= 3
    for resource in assets:
        loaded = client.get(f"/{page}/{resource}")
        assert loaded.status_code == 200
        assert "no-store" in loaded.headers["cache-control"]


@pytest.mark.parametrize("path", ["/kiosk/", "/kiosk/voice.js", "/kiosk/styles.css"])
def test_stale_conditional_requests_do_not_return_304(client, path):
    first = client.get(path)
    stale_headers = {"If-Modified-Since": "Wed, 01 Jan 2099 00:00:00 GMT"}
    if "etag" in first.headers:
        stale_headers["If-None-Match"] = first.headers["etag"]
    response = client.get(path, headers=stale_headers)
    assert response.status_code == 200
    assert response.content
    assert "no-store" in response.headers["cache-control"]


def test_resource_url_changes_with_contents_even_when_timestamp_and_size_do_not(tmp_path):
    (tmp_path / "index.html").write_text('<script src="app.js"></script>', encoding="utf-8")
    asset = tmp_path / "app.js"
    asset.write_text("old-code", encoding="utf-8")
    previous = asset.stat()
    isolated = FastAPI()
    isolated.mount("/", AppStaticFiles(directory=tmp_path, html=True))
    with TestClient(isolated) as browser:
        first = browser.get("/").text
        # 模拟缓存持有相同 URL 的旧内容，部署内容同长度、同文件时间。
        asset.write_text("new-code", encoding="utf-8")
        os.utime(asset, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        second = browser.get("/").text
        assert first != second
        old_url = re.search(r'src="([^"]+)"', first)[1]
        new_url = re.search(r'src="([^"]+)"', second)[1]
        cached = {old_url: "old-code"}
        assert new_url not in cached
        assert browser.get("/" + new_url).text == "new-code"
