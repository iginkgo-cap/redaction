"""Microsoft Graph 取件（OAuth，Mail.ReadWrite）。列收件箱 → `/messages/{id}/$value` 取整封 MIME → 加分类标记。
全部请求带 `Prefer: IdType="ImmutableId"`，id 不随移动文件夹变化（设计 D18）。游标 {received, id}。"""
from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import date, datetime, timezone
from typing import Callable

from file_masker.mail.config import Mailbox
from file_masker.mail.httpclient import Http
from file_masker.mail.source import Candidate, Scan

log = logging.getLogger("file_masker.mail.graph")

GRAPH = "https://graph.microsoft.com/v1.0"
PROCESSED_CATEGORY = "file-masker-processed"   # 用分类而不是 Java 用的后续标记，两边互不干扰
PAGE_SIZE = 50
_FRACTION = re.compile(r"\.(\d{1,9})")


def _parse_iso(value: str) -> datetime:
    """Graph 返回如 2026-10-16T09:51:00.1234567Z（7 位小数）；Python 只认 6 位且不认 Z。"""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    value = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _graph_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GraphSource:
    def __init__(self, mailbox: Mailbox, token_provider: Callable[[], str], http: Http,
                 is_processed: Callable[[str], bool]) -> None:
        self._box = mailbox
        self._token = token_provider
        self._http = http
        self._is_processed = is_processed

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def initial_cursor(self, since: date | None) -> dict:
        start = datetime(since.year, since.month, since.day, tzinfo=timezone.utc) if since else datetime.now(timezone.utc)
        return {"received": start.isoformat(), "id": ""}

    @staticmethod
    def cursor_time(cursor: dict) -> datetime:
        return _parse_iso(cursor["received"])

    def scan(self, cursor: dict, window: tuple[datetime, datetime]) -> Scan:
        start, end = window
        params = {
            # $orderby 的字段必须按相同顺序出现在 $filter 里，否则 InefficientFilter；所以只按时间排，本地再按 (时间, id)
            "$filter": f"receivedDateTime ge {_graph_time(start)} and receivedDateTime lt {_graph_time(end)}",
            "$select": "id,receivedDateTime",
            "$orderby": "receivedDateTime",
            "$top": PAGE_SIZE,
        }
        url: str | None = f"{GRAPH}/me/mailFolders/inbox/messages?{urllib.parse.urlencode(params)}"
        candidates = []
        while url:
            body = self._http.get_json(url, self._headers())
            for item in body.get("value", []):
                mail_id = item["id"]
                if self._is_processed(mail_id):
                    continue
                ts = _parse_iso(item["receivedDateTime"])
                candidates.append(Candidate(mail_id, ts, {"received": ts.isoformat(), "id": mail_id}))
            url = body.get("@odata.nextLink")
        candidates.sort(key=lambda c: (c.ts, c.mail_id))
        return Scan(candidates, {"received": end.astimezone(timezone.utc).isoformat(), "id": ""})

    def fetch(self, mail_id: str) -> bytes:
        return self._http.get_bytes(f"{GRAPH}/me/messages/{_quote(mail_id)}/$value", self._headers())

    def mark_done(self, mail_id: str) -> None:
        # 标记前重新读一次分类再并集，PATCH 是整体替换（D18）
        current = self._http.get_json(f"{GRAPH}/me/messages/{_quote(mail_id)}?$select=categories", self._headers())
        categories = list(current.get("categories") or [])
        if PROCESSED_CATEGORY in categories:
            return
        self._http.patch_json(f"{GRAPH}/me/messages/{_quote(mail_id)}",
                              {"categories": categories + [PROCESSED_CATEGORY]}, self._headers())

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}", "Prefer": 'IdType="ImmutableId"'}


def _quote(mail_id: str) -> str:
    return urllib.parse.quote(mail_id, safe="")
