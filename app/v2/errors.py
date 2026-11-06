"""v2 错误码（ADR-002 §2.3、ADR-003 §3）：body {code, message} 为权威，同值放 X-Redaction-Error 头。"""
from __future__ import annotations

_STATUS = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "text_too_large": 413,
    "file_too_large": 413,
    "file_unreadable": 422,
    "detect_failed": 500,
    "storage_unavailable": 503,
    "overloaded": 503,
    "not_ready": 503,
}
_RETRY_AFTER = {"storage_unavailable", "overloaded"}


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status(self) -> int:
        return _STATUS[self.code]

    @property
    def headers(self) -> dict[str, str]:
        h = {"X-Redaction-Error": self.code}
        if self.code in _RETRY_AFTER:
            h["Retry-After"] = "1"
        return h
