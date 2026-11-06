import imaplib
from datetime import date, datetime, timezone

import pytest

from file_masker.mail import source_imap
from file_masker.mail.config import Mailbox
from file_masker.mail.source import Candidate, Scan
from file_masker.mail.source_imap import ImapSource

MAILBOX = Mailbox(tenant="demo", address="src@example.com", provider="imap", sink="smtp", password="pw",
                  imap_host="imap.x", imap_port=993, smtp_host="smtp.x", smtp_port=465,
                  smtp_to="dst@example.com", since=None)
WINDOW = (datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc), datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc))


class FakeImap:
    """按 imaplib 的返回格式模拟。"""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.args, self.commands, self.status = (host, port), [], "OK"
        self.uidvalidity, self.uidnext = 7, 42
        self.mails = {40: "15-Oct-2026 18:00:00 +0800", 41: "16-Oct-2026 09:30:00 +0800"}  # uid -> INTERNALDATE
        self.since_result = [41]
        FakeImap.instances.append(self)

    def login(self, user, password):
        self.commands.append(("LOGIN", user, password))
        return "OK", [b"ok"]

    def select(self, mailbox):
        self.commands.append(("SELECT", mailbox))
        self._responses = {"UIDVALIDITY": [str(self.uidvalidity).encode()], "UIDNEXT": [str(self.uidnext).encode()]}
        return "OK", [b"2"]

    def response(self, code):
        return code, self._responses.get(code, [None])

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            if args[0] == "SINCE":
                return self.status, [" ".join(map(str, self.since_result)).encode()]
            lo = int(args[1].split(":")[0])  # "UID lo:*"
            uids = [u for u in sorted(self.mails) if u >= lo] or [max(self.mails)]  # RFC: n:* 超过最大 UID 仍返回最后一封
            return self.status, [" ".join(map(str, uids)).encode()]
        if command == "FETCH" and args[1] == "(UID INTERNALDATE)":
            lines = [f'{i} (UID {u} INTERNALDATE "{self.mails[u]}")'.encode()
                     for i, u in enumerate(int(x) for x in args[0].split(","))]
            return self.status, lines
        if command == "FETCH":
            return self.status, [(b"1 (UID %s BODY[] {5}" % args[0].encode(), b"hello"), b")"]
        return self.status, [b"done"]

    def logout(self):
        self.commands.append(("LOGOUT",))


@pytest.fixture
def src(monkeypatch):
    FakeImap.instances.clear()
    monkeypatch.setattr(source_imap.imaplib, "IMAP4_SSL", FakeImap)
    s = ImapSource(MAILBOX)
    s.open()
    return s, FakeImap.instances[0]


def test_open_logs_in_and_selects(src):
    _, conn = src
    assert conn.args == ("imap.x", 993)
    assert conn.commands[:2] == [("LOGIN", "src@example.com", "pw"), ("SELECT", "INBOX")]


def test_initial_cursor_without_since_is_uidnext_minus_one(src):
    s, _ = src
    assert s.initial_cursor(None) == {"uidvalidity": 7, "last_uid": 41}


def test_initial_cursor_with_since_backfills(src):
    s, conn = src
    assert s.initial_cursor(date(2026, 10, 16)) == {"uidvalidity": 7, "last_uid": 40}
    assert conn.commands[-1] == ("SEARCH", "SINCE", "16-Oct-2026")
    conn.since_result = []
    assert s.initial_cursor(date(2026, 10, 16)) == {"uidvalidity": 7, "last_uid": 41}


def test_scan_returns_new_uids_sorted_with_internaldate(src):
    s, conn = src
    scan = s.scan({"uidvalidity": 7, "last_uid": 39}, WINDOW)
    assert isinstance(scan, Scan)
    assert [c.mail_id for c in scan.candidates] == ["7-40", "7-41"]
    assert scan.candidates[0].ts == datetime(2026, 10, 15, 18, 0, tzinfo=timezone(__import__("datetime").timedelta(hours=8)))
    assert scan.candidates[1].cursor == {"uidvalidity": 7, "last_uid": 41}
    assert all(isinstance(c, Candidate) and c.raw is None for c in scan.candidates)
    assert scan.cursor_after == {"uidvalidity": 7, "last_uid": 41}
    assert ("SEARCH", "UID", "40:*") in conn.commands
    assert ("FETCH", "40,41", "(UID INTERNALDATE)") in conn.commands


def test_scan_filters_star_range_quirk(src):
    s, conn = src
    scan = s.scan({"uidvalidity": 7, "last_uid": 41}, WINDOW)  # 42:* 会返回最后一封 41
    assert scan.candidates == [] and scan.cursor_after == {"uidvalidity": 7, "last_uid": 41}
    assert ("SEARCH", "UID", "42:*") in conn.commands
    assert not any(c[0] == "FETCH" for c in conn.commands)


def test_uidvalidity_change_resets_cursor_without_replay(src, caplog):
    s, _ = src
    scan = s.scan({"uidvalidity": 3, "last_uid": 999}, WINDOW)
    assert scan.candidates == [] and scan.cursor_after == {"uidvalidity": 7, "last_uid": 41}
    assert "UIDVALIDITY" in caplog.text


def test_fetch_peeks_and_mark_done_flags(src):
    s, conn = src
    assert s.fetch("7-41") == b"hello"
    assert conn.commands[-1] == ("FETCH", "41", "(BODY.PEEK[])")
    s.mark_done("7-41")
    assert conn.commands[-1] == ("STORE", "41", "+FLAGS", "(\\Flagged)")


def test_fetch_rejects_foreign_epoch(src):
    s, _ = src
    with pytest.raises(ValueError):
        s.fetch("3-41")


def test_not_ok_raises(src):
    s, conn = src
    conn.status = "NO"
    with pytest.raises(imaplib.IMAP4.error):
        s.mark_done("7-41")


def test_close_logs_out_once(src):
    s, conn = src
    s.close()
    s.close()
    assert conn.commands.count(("LOGOUT",)) == 1
