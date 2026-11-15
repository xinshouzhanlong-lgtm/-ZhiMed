"""应用页面按内容版本加载脚本/样式，避免新 HTML 混用旧缓存。"""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

_ASSET_REF = re.compile(r'''(?P<prefix>\b(?:src|href)=["'])(?P<url>[^"']+)(?P<suffix>["'])''')
_NO_STORE = "no-store, max-age=0"


def _versioned_html(html_path: Path, root: Path) -> str:
    def version(match: re.Match) -> str:
        url = urlsplit(match["url"])
        if url.scheme or url.netloc or url.path.startswith("/"):
            return match[0]
        asset = (html_path.parent / unquote(url.path)).resolve()
        if not asset.is_relative_to(root) or asset.suffix not in {".js", ".css"} or not asset.is_file():
            return match[0]
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:16]
        query = [(key, value) for key, value in parse_qsl(url.query) if key != "v"]
        query.append(("v", digest))
        reference = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), url.fragment))
        return match["prefix"] + reference + match["suffix"]

    return _ASSET_REF.sub(version, html_path.read_text(encoding="utf-8"))


class AppStaticFiles(StaticFiles):
    """应用资源每次取新内容；医生头像仍走独立的 immutable 缓存接口。"""

    def is_not_modified(self, response_headers, request_headers) -> bool:
        # 不返回 304：浏览器可能持有与当前 HTML 不兼容的旧脚本。
        return False

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if isinstance(response, FileResponse) and response.status_code == 200:
            file_path = Path(response.path)
            if file_path.suffix == ".html":
                html = await asyncio.to_thread(_versioned_html, file_path, Path(self.directory).resolve())
                response = HTMLResponse(html)
        response.headers["Cache-Control"] = _NO_STORE
        return response
