import base64
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from file_masker.mail.config import Mailbox
from file_masker.mail.httpclient import HttpError, Unauthorized
from file_masker.mail.source_gmail import GmailSource, PROCESSED_LABEL

MAILBOX = Mailbox(tenant="lq", address="lq@example.com", provider="gmail", sink="fs", password=None,
                  imap_host="", imap_port=993, smtp_host="", smtp_port=465, smtp_to=None, since=None)
START = datetime(2026, 10, 16, 9, 50, tzinfo=timezone.utc)
END = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
RAW_A = b"From: a@x\r\nSubject: A\r\n\r\nbody A\r\n"
RAW_B = b"From: b@x\r\nSubject: B\r\n\r\nbody B\r\n"


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeHttp:
    """按 URL 路由的假 Gmail API；记录调用。"""

    def __init__(self):
        self.calls = []
        self.pages = [{"messages": [{"id": "idB"}, {"id": "idA"}], "nextPageToken": "p2"},
                      {"messages": [{"id": "idOld"}]}]
        self.messages = {
            "idA": {"id": "idA", "internalDate": str(int(START.timestamp() * 1000) + 1000), "raw": b64(RAW_A)},
            "idB": {"id": "idB", "internalDate": str(int(START.timestamp() * 1000) + 5000), "raw": b64(RAW_B)},
            "idOld": {"id": "idOld", "internalDate": "1", "raw": b64(b"old")},
        }
        self.labels = {"labels": [{"id": "Label_1", "name": "Other"}]}
        self.fail_with = None

    def _auth(self, headers):
        assert headers.get("Authorization") == "Bearer tok", headers
        if self.fail_with:
            raise self.fail_with

    def get_json(self, url, headers=None):
        self._auth(headers or {})
        self.calls.append(("GET", url))
        path, q = urlparse(url).path, parse_qs(urlparse(url).query)
        if path.endswith("/messages"):
            page = int(q.get("pageToken", ["p1"])[0][1:]) - 1
            return self.pages[page]
        if "/messages/" in path:
            return self.messages[path.rsplit("/", 1)[1]]
        if path.endswith("/labels"):
            return self.labels
        raise AssertionError(url)

    def post_json(self, url, body, headers=None):
        self._auth(headers or {})
        self.calls.append(("POST", url, body))
        if url.endswith("/labels"):
            self.labels["labels"].append({"id": "Label_9", "name": body["name"]})
            return {"id": "Label_9", "name": body["name"]}
        return {}


@pytest.fixture
def src():
    http = FakeHttp()
    processed = {"idOld"}
    s = GmailSource(MAILBOX, lambda: "tok", http, lambda mail_id: mail_id in processed)
    s.open()
    return s, http


def test_initial_cursor(src):
    s, _ = src
    assert s.initial_cursor(date(2026, 10, 1)) == {"ts_ms": int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000), "id": ""}
    c = s.initial_cursor(None)
    assert abs(c["ts_ms"] / 1000 - datetime.now(timezone.utc).timestamp()) < 5 and c["id"] == ""
    assert s.cursor_time(c).tzinfo is not None


def test_scan_lists_window_pages_and_fetches_raw_only_for_unprocessed(src):
    s, http = src
    scan = s.scan({"ts_ms": 0, "id": ""}, (START, END))
    list_calls = [u for m, u in http.calls if m == "GET" and urlparse(u).path.endswith("/messages")]
    assert len(list_calls) == 2
    q = parse_qs(urlparse(list_calls[0]).query)
    assert q["q"] == [f"in:inbox after:{int(START.timestamp())} before:{int(END.timestamp())}"]
    assert parse_qs(urlparse(list_calls[1]).query)["pageToken"] == ["p2"]
    # 已处理的 idOld 不再 GET；候选按 (ts, id) 升序；扫描只取 internalDate，原文留给 fetch
    assert [c.mail_id for c in scan.candidates] == ["idA", "idB"]
    assert scan.candidates[0].raw is None and scan.candidates[1].raw is None
    assert scan.candidates[0].ts == datetime.fromtimestamp(START.timestamp() + 1, tz=timezone.utc)
    assert scan.candidates[0].cursor == {"ts_ms": int(START.timestamp() * 1000) + 1000, "id": "idA"}
    assert scan.cursor_after == {"ts_ms": int(END.timestamp() * 1000), "id": ""}
    fetched = [u for m, u in http.calls if m == "GET" and "/messages/" in u]
    assert sorted(u.rsplit("/", 1)[1].split("?")[0] for u in fetched) == ["idA", "idB"]
    assert all("format=minimal" in u for u in fetched)


def test_fetch_and_mark_done_creates_label_once(src):
    s, http = src
    assert s.fetch("idA") == RAW_A
    s.mark_done("idA")
    s.mark_done("idB")
    posts = [c for c in http.calls if c[0] == "POST"]
    assert posts[0][1].endswith("/labels") and posts[0][2]["name"] == PROCESSED_LABEL
    assert posts[1][1].endswith("/messages/idA/modify") and posts[1][2] == {"addLabelIds": ["Label_9"]}
    assert posts[2][1].endswith("/messages/idB/modify")
    assert sum(1 for c in posts if c[1].endswith("/labels")) == 1  # 标签只创建一次


def test_unauthorized_propagates(src):
    s, http = src
    http.fail_with = Unauthorized("x")
    with pytest.raises(Unauthorized):
        s.scan({"ts_ms": 0, "id": ""}, (START, END))


def test_other_http_errors_propagate(src):
    s, http = src
    http.fail_with = HttpError(503, "down")
    with pytest.raises(HttpError):
        s.scan({"ts_ms": 0, "id": ""}, (START, END))
