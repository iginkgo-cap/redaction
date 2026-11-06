"""Gmail API 取件（OAuth）。与 Java 侧 GmailMailFetcher 同一种接法：messages.list → messages.get(format=raw) → messages.modify。
游标 {ts_ms, id}；扫描按时间窗口（`after:`/`before:` 秒）；已处理的 id 不再 GET，未处理的只取 internalDate（format=minimal），
整封原文由 worker 决定要处理时再 fetch。"""
from __future__ import annotations

import base64
import logging
import urllib.parse
from datetime import date, datetime, timezone
from typing import Callable

from file_masker.mail.config import Mailbox
from file_masker.mail.httpclient import Http
from file_masker.mail.source import Candidate, Scan

log = logging.getLogger("file_masker.mail.gmail")

API = "https://gmail.googleapis.com/gmail/v1/users/me"
PROCESSED_LABEL = "file-masker-processed"   # 与 Java 的 Ginkgo-Processed 不同名，两边互不干扰
PAGE_SIZE = 100


class GmailSource:
    def __init__(self, mailbox: Mailbox, token_provider: Callable[[], str], http: Http,
                 is_processed: Callable[[str], bool]) -> None:
        self._box = mailbox
        self._token = token_provider
        self._http = http
        self._is_processed = is_processed
        self._label_id: str | None = None

    def open(self) -> None:
        pass  # 无长连接；标签 id 在首次标记时惰性获取

    def close(self) -> None:
        pass

    def initial_cursor(self, since: date | None) -> dict:
        start = datetime(since.year, since.month, since.day, tzinfo=timezone.utc) if since else datetime.now(timezone.utc)
        return {"ts_ms": int(start.timestamp() * 1000), "id": ""}

    @staticmethod
    def cursor_time(cursor: dict) -> datetime:
        return datetime.fromtimestamp(int(cursor["ts_ms"]) / 1000, tz=timezone.utc)

    def scan(self, cursor: dict, window: tuple[datetime, datetime]) -> Scan:
        start, end = window
        query = f"in:inbox after:{int(start.timestamp())} before:{int(end.timestamp())}"
        ids: list[str] = []
        page_token = None
        while True:
            params = {"q": query, "maxResults": PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            body = self._get(f"{API}/messages?{urllib.parse.urlencode(params)}")
            ids += [m["id"] for m in body.get("messages", [])]
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        candidates = []
        for mail_id in ids:
            if self._is_processed(mail_id):
                continue
            msg = self._get(f"{API}/messages/{urllib.parse.quote(mail_id, safe='')}?format=minimal")
            ts_ms = int(msg["internalDate"])
            candidates.append(Candidate(mail_id, datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                                        {"ts_ms": ts_ms, "id": mail_id}))
        candidates.sort(key=lambda c: (c.ts, c.mail_id))
        return Scan(candidates, {"ts_ms": int(end.timestamp() * 1000), "id": ""})

    def fetch(self, mail_id: str) -> bytes:
        msg = self._get(f"{API}/messages/{urllib.parse.quote(mail_id, safe='')}?format=raw")
        return _b64url_decode(msg["raw"])

    def mark_done(self, mail_id: str) -> None:
        self._http.post_json(f"{API}/messages/{urllib.parse.quote(mail_id, safe='')}/modify",
                             {"addLabelIds": [self._ensure_label()]}, self._headers())

    def _ensure_label(self) -> str:
        if self._label_id is None:
            labels = self._get(f"{API}/labels").get("labels", [])
            match = next((l["id"] for l in labels if l.get("name") == PROCESSED_LABEL), None)
            if match is None:
                created = self._http.post_json(
                    f"{API}/labels",
                    {"name": PROCESSED_LABEL, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
                    self._headers())
                match = created["id"]
            self._label_id = match
        return self._label_id

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def _get(self, url: str) -> dict:
        return self._http.get_json(url, self._headers())


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
