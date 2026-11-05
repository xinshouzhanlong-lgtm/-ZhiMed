"""错误码 → HTTP 状态映射（统一出口，避免各处硬编码）。

约定：E_* 前缀为领域错误码；未知码一律 400，不泄露内部细节。
"""
from __future__ import annotations

_STATUS = {
    "E_TOO_LARGE": 413,
    "E_BAD_MAGIC": 415,
    "E_SVG_REJECTED": 415,
    "E_BAD_DIMENSION": 422,
    "E_DECODE_ERROR": 422,
    "E_CONSENT_REQUIRED": 422,
    "E_NO_CHANGE": 422,
    "E_LOW_CONFIDENCE": 422,
    "E_FIELD_NOT_ALLOWED": 422,
    "E_FIELD_NOT_FOUND": 422,
    "E_FORBIDDEN": 403,
    "E_DEPT_NOT_FOUND": 404,
    "E_AMBIGUOUS_SECTION": 409,
    "E_STALE_BASE": 409,
    "E_LOCK_TIMEOUT": 409,
    "E_NO_TOKEN": 403,
    "E_VERIFY_FAILED": 500,
    "E_PATH_DENIED": 403,
}


def http_status_for(code: str | None) -> int:
    return _STATUS.get(code or "", 400)
