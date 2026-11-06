"""IMAP 取件（密码登录，SSL）。每轮新建连接。mail_id = "<uidvalidity>-<uid>"，游标 {uidvalidity, last_uid}。"""
from __future__ import annotations

import imaplib
import logging
import re
from datetime import date, datetime

from file_masker.mail.config import Mailbox
from file_masker.mail.source import Candidate, Scan

log = logging.getLogger("file_masker.mail.imap")

TIMEOUT_S = 30
_INTERNALDATE = re.compile(rb'UID (\d+).*INTERNALDATE "([^"]+)"')


def _ok(typ: str, what: str) -> None:
    if typ != "OK":
        raise imaplib.IMAP4.error(f"IMAP {what} failed: {typ}")


class ImapSource:
    def __init__(self, mailbox: Mailbox) -> None:
        self._box = mailbox
        self._conn: imaplib.IMAP4_SSL | None = None
        self._uidvalidity = 0
        self._uidnext = 1

    def open(self) -> None:
        conn = imaplib.IMAP4_SSL(self._box.imap_host, self._box.imap_port, timeout=TIMEOUT_S)
        self._conn = conn
        _ok(conn.login(self._box.address, self._box.password)[0], "login")
        _ok(conn.select("INBOX")[0], "select")
        self._uidvalidity = int(conn.response("UIDVALIDITY")[1][0])
        self._uidnext = int(conn.response("UIDNEXT")[1][0])

    def initial_cursor(self, since: date | None) -> dict:
        last = self._uidnext - 1
        if since is not None:
            uids = self._search("SINCE", since.strftime("%d-%b-%Y"))
            if uids:
                last = min(uids) - 1
        return {"uidvalidity": self._uidvalidity, "last_uid": last}

    @staticmethod
    def cursor_time(cursor: dict) -> datetime | None:
        return None  # 按 UID 取件，不用时间窗口

    def scan(self, cursor: dict, window: tuple[datetime, datetime]) -> Scan:
        if cursor.get("uidvalidity") != self._uidvalidity:  # 邮箱重建：换 epoch，不重放历史（E10）
            log.warning("UIDVALIDITY changed for %s: %s -> %s; cursor reset", self._box.address,
                        cursor.get("uidvalidity"), self._uidvalidity)
            return Scan([], {"uidvalidity": self._uidvalidity, "last_uid": self._uidnext - 1})
        last = int(cursor["last_uid"])
        # 「UID n:*」在 n 超过最大 UID 时仍会返回最后一封（RFC 9051 §6.4.9），必须再过滤
        uids = sorted(u for u in self._search("UID", f"{last + 1}:*") if u > last)
        candidates = []
        if uids:
            typ, data = self._conn.uid("FETCH", ",".join(map(str, uids)), "(UID INTERNALDATE)")
            _ok(typ, "fetch internaldate")
            dates = {}
            for item in data or []:
                line = item[0] if isinstance(item, tuple) else item
                m = _INTERNALDATE.search(line or b"")
                if m:
                    dates[int(m.group(1))] = datetime.strptime(m.group(2).decode(), "%d-%b-%Y %H:%M:%S %z")
            for uid in uids:
                if uid not in dates:
                    continue
                candidates.append(Candidate(f"{self._uidvalidity}-{uid}", dates[uid],
                                            {"uidvalidity": self._uidvalidity, "last_uid": uid}))
        cursor_after = {"uidvalidity": self._uidvalidity, "last_uid": max(uids) if uids else last}
        return Scan(candidates, cursor_after)

    def fetch(self, mail_id: str) -> bytes:
        uid = self._uid(mail_id)
        typ, data = self._conn.uid("FETCH", str(uid), "(BODY.PEEK[])")  # PEEK：不改已读状态
        _ok(typ, "fetch")
        for item in data or []:
            if isinstance(item, tuple):
                return item[1]
        raise imaplib.IMAP4.error(f"IMAP fetch returned no body for uid {uid}")

    def mark_done(self, mail_id: str) -> None:
        _ok(self._conn.uid("STORE", str(self._uid(mail_id)), "+FLAGS", "(\\Flagged)")[0], "store")

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass

    def _uid(self, mail_id: str) -> int:
        epoch, _, uid = mail_id.partition("-")
        if int(epoch) != self._uidvalidity:
            raise ValueError(f"mail id {mail_id} belongs to another UIDVALIDITY epoch")
        return int(uid)

    def _search(self, *criteria: str) -> list[int]:
        typ, data = self._conn.uid("SEARCH", *criteria)
        _ok(typ, "search")
        return [int(x) for x in data[0].split()] if data and data[0] else []
