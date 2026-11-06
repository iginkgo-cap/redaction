"""极简 HTTP 客户端（标准库 urllib）：OAuth 与 Gmail / Graph 调用共用。超时 30 秒，不重试；日志不打印请求体与头。"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

log = logging.getLogger("file_masker.mail.http")

TIMEOUT_S = 30


class HttpError(Exception):
    def __init__(self, status: int, body: object) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class Unauthorized(HttpError):
    def __init__(self, url: str, body: object = None) -> None:
        HttpError.__init__(self, 401, body)   # body 保留：OAuth token 端点的 401 带 AADSTS 错误码
        self.url = url


class Http(Protocol):
    def post_form(self, url: str, data: dict, headers: dict | None = None) -> dict: ...
    def get_json(self, url: str, headers: dict | None = None) -> dict: ...
    def get_bytes(self, url: str, headers: dict | None = None) -> bytes: ...
    def post_json(self, url: str, body: dict, headers: dict | None = None) -> dict: ...
    def patch_json(self, url: str, body: dict, headers: dict | None = None) -> dict: ...


class UrllibHttp:
    def _call(self, method: str, url: str, data: bytes | None, headers: dict) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 401:
                raise Unauthorized(url, _maybe_json(body)) from None
            raise HttpError(e.code, _maybe_json(body)) from None

    def post_form(self, url: str, data: dict, headers: dict | None = None) -> dict:
        encoded = urllib.parse.urlencode(data).encode()
        h = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", **(headers or {})}
        _, body = self._call("POST", url, encoded, h)
        return _maybe_json(body) or {}

    def get_json(self, url: str, headers: dict | None = None) -> dict:
        _, body = self._call("GET", url, None, {"Accept": "application/json", **(headers or {})})
        return _maybe_json(body) or {}

    def get_bytes(self, url: str, headers: dict | None = None) -> bytes:
        _, body = self._call("GET", url, None, headers or {})
        return body

    def post_json(self, url: str, body: dict, headers: dict | None = None) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
        _, out = self._call("POST", url, json.dumps(body).encode(), h)
        return _maybe_json(out) or {}

    def patch_json(self, url: str, body: dict, headers: dict | None = None) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
        _, out = self._call("PATCH", url, json.dumps(body).encode(), h)
        return _maybe_json(out) or {}


def _maybe_json(body: bytes) -> object:
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")
