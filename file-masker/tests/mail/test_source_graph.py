from datetime import date, datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from file_masker.mail.config import Mailbox
from file_masker.mail.httpclient import Unauthorized
from file_masker.mail.source_graph import PROCESSED_CATEGORY, GraphSource

MAILBOX = Mailbox(tenant="look", address="ops@look.example", provider="graph", sink="fs", password=None,
                  imap_host="", imap_port=993, smtp_host="", smtp_port=465, smtp_to=None, since=None)
START = datetime(2026, 10, 16, 9, 50, tzinfo=timezone.utc)
END = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
ODD_ID = "AAMkAGI2/abc=="  # 不可变 id 可能含 / 和 =


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.page1 = {"value": [
            {"id": "m-late", "receivedDateTime": "2026-10-16T09:55:00Z"},
            {"id": "m-early", "receivedDateTime": "2026-10-16T09:51:00.1234567Z"},
        ], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skip=2&token=p2"}
        self.page2 = {"value": [
            {"id": "m-done", "receivedDateTime": "2026-10-16T09:52:00Z"},
            {"id": ODD_ID, "receivedDateTime": "2026-10-16T09:55:00Z"},  # 与 m-late 同时间：按 id 排序
        ]}
        self.categories = {"m-early": ["Blue category"]}
        self.fail_with = None

    def _check(self, headers):
        assert headers.get("Authorization") == "Bearer tok"
        assert headers.get("Prefer") == 'IdType="ImmutableId"'
        if self.fail_with:
            raise self.fail_with

    def get_json(self, url, headers=None):
        self._check(headers or {})
        self.calls.append(("GET", url))
        if "token=p2" in url:
            return self.page2
        if "/mailFolders/inbox/messages" in url:
            return self.page1
        if "$select=categories" in url:
            mid = unquote(urlparse(url).path.split("/messages/")[1].split("/")[0])
            return {"id": mid, "categories": self.categories.get(mid, [])}
        raise AssertionError(url)

    def get_bytes(self, url, headers=None):
        self._check(headers or {})
        self.calls.append(("GETB", url))
        return b"From: a@x\r\nSubject: MIME\r\n\r\nbody\r\n"

    def patch_json(self, url, body, headers=None):
        self._check(headers or {})
        self.calls.append(("PATCH", url, body))
        return {}


@pytest.fixture
def src():
    http = FakeHttp()
    s = GraphSource(MAILBOX, lambda: "tok", http, lambda mail_id: mail_id == "m-done")
    s.open()
    return s, http


def test_initial_cursor_and_cursor_time(src):
    s, _ = src
    c = s.initial_cursor(date(2026, 10, 1))
    assert c == {"received": "2026-10-01T00:00:00+00:00", "id": ""}
    assert s.cursor_time(c) == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_scan_query_shape_pagination_and_local_sort(src):
    s, http = src
    scan = s.scan({"received": "2026-10-16T09:40:00+00:00", "id": ""}, (START, END))
    first = urlparse(http.calls[0][1])
    assert first.path == "/v1.0/me/mailFolders/inbox/messages"
    q = {k: v[0] for k, v in parse_qs(first.query).items()}
    assert q["$filter"] == "receivedDateTime ge 2026-10-16T09:50:00Z and receivedDateTime lt 2026-10-16T10:00:00Z"
    assert q["$orderby"] == "receivedDateTime"  # 不能含 id：Graph 要求 $orderby 字段必须出现在 $filter 里
    assert q["$select"] == "id,receivedDateTime" and q["$top"] == "50"
    assert http.calls[1][1].endswith("token=p2")  # 跟 nextLink
    # 已处理的 m-done 被排除；本地按 (时间, id) 排序；同一时间按 id
    assert [c.mail_id for c in scan.candidates] == ["m-early", ODD_ID, "m-late"]
    assert scan.candidates[0].ts == datetime(2026, 10, 16, 9, 51, 0, 123456, tzinfo=timezone.utc)
    assert scan.candidates[0].cursor == {"received": "2026-10-16T09:51:00.123456+00:00", "id": "m-early"}
    assert all(c.raw is None for c in scan.candidates)
    assert scan.cursor_after == {"received": "2026-10-16T10:00:00+00:00", "id": ""}


def test_fetch_percent_encodes_id(src):
    s, http = src
    assert s.fetch(ODD_ID).startswith(b"From: a@x")
    path = urlparse(http.calls[-1][1]).path
    assert path == "/v1.0/me/messages/AAMkAGI2%2Fabc%3D%3D/$value"


def test_mark_done_merges_categories_after_fresh_get(src):
    s, http = src
    s.mark_done("m-early")
    get, patch = http.calls[-2], http.calls[-1]
    assert get[0] == "GET" and "$select=categories" in get[1]
    assert patch[0] == "PATCH" and urlparse(patch[1]).path == "/v1.0/me/messages/m-early"
    assert patch[2] == {"categories": ["Blue category", PROCESSED_CATEGORY]}
    s.mark_done("m-late")  # 无已有分类
    assert http.calls[-1][2] == {"categories": [PROCESSED_CATEGORY]}


def test_mark_done_is_idempotent_when_category_present(src):
    s, http = src
    http.categories["m-late"] = [PROCESSED_CATEGORY]
    s.mark_done("m-late")
    assert http.calls[-1][0] == "GET"  # 已有分类则不再 PATCH


def test_unauthorized_propagates(src):
    s, http = src
    http.fail_with = Unauthorized("x")
    with pytest.raises(Unauthorized):
        s.scan({"received": "2026-10-16T09:40:00+00:00", "id": ""}, (START, END))
