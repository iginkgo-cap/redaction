"""邮件模式的持久状态：SQLite（WAL）。游标、已处理记录、按租户的规则、OAuth 凭据、审计。

写入方有三个线程（worker、Web、授权后台），每线程一个连接，`busy_timeout` 等锁，事务短小；
网络调用不进事务。库里不存原文：`processed.subject` 是打码后的标题。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
RECENT_LIMIT = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox_state (
  address        TEXT PRIMARY KEY,
  cursor         TEXT NOT NULL,
  not_before     TEXT NOT NULL,
  scan_until     TEXT,
  last_wide_scan TEXT,
  first_run_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed (
  address     TEXT NOT NULL,
  mail_id     TEXT NOT NULL,
  tenant      TEXT NOT NULL,
  status      TEXT NOT NULL,
  reason      TEXT NOT NULL DEFAULT '',
  subject     TEXT NOT NULL DEFAULT '',
  hits        INTEGER NOT NULL DEFAULT 0,
  attachments INTEGER NOT NULL DEFAULT 0,
  location    TEXT NOT NULL DEFAULT '',
  at          TEXT NOT NULL,
  skipped     INTEGER NOT NULL DEFAULT 0,
  original    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (address, mail_id)
);
CREATE INDEX IF NOT EXISTS idx_processed_tenant_at ON processed(tenant, at);
CREATE TABLE IF NOT EXISTS rules (
  tenant   TEXT NOT NULL,
  position INTEGER NOT NULL,
  source   TEXT NOT NULL,
  target   TEXT NOT NULL,
  PRIMARY KEY (tenant, position)
);
CREATE TABLE IF NOT EXISTS oauth_token (
  address         TEXT PRIMARY KEY,
  provider        TEXT NOT NULL,
  account         TEXT NOT NULL,
  refresh_token   TEXT NOT NULL,
  granted_at      TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'ok',
  auth_generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_generation (   -- 断开后 token 行已删，generation 仍要保留给在途流程核对
  address    TEXT PRIMARY KEY,
  generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
  at     TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  client TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class MailboxState:
    address: str
    cursor: dict
    not_before: str
    scan_until: str | None
    last_wide_scan: str | None
    first_run_at: str


@dataclass(frozen=True)
class ProcessedRow:
    address: str
    mail_id: str
    tenant: str
    status: str          # written / sent / held
    reason: str
    subject: str         # 打码后的标题
    hits: int
    attachments: int
    location: str
    at: str
    skipped: int = 0     # 没能打码、没落盘的附件数
    original: str = ""   # 整封原件的存放路径；没存为空


# 1.3.1 之前建的库没有这些列，打开时补上
_ADDED_COLUMNS = {"processed": (("skipped", "INTEGER NOT NULL DEFAULT 0"), ("original", "TEXT NOT NULL DEFAULT ''"))}


@dataclass(frozen=True)
class TokenRow:
    address: str
    provider: str
    account: str
    refresh_token: str
    granted_at: str
    status: str          # ok / expired
    auth_generation: int


@dataclass(frozen=True)
class AuditRow:
    at: str
    action: str
    target: str
    client: str


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class StateDb:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._local = threading.local()
        old_umask = os.umask(0o077)  # 建库文件只给自己（设计 D16）
        try:
            self.connection().executescript(SCHEMA)  # executescript 自带提交，不能再套事务
            self._add_missing_columns()
        finally:
            os.umask(old_umask)
        self._ensure_private()

    def _add_missing_columns(self) -> None:
        conn = self.connection()
        for table, columns in _ADDED_COLUMNS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ---- 连接 ----

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _ensure_private(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists() and (p.stat().st_mode & 0o777) != 0o600:
                os.chmod(p, 0o600)

    class _Tx:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def __enter__(self) -> sqlite3.Connection:
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb) -> None:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")

    def _transaction(self) -> "StateDb._Tx":
        return StateDb._Tx(self.connection())

    # ---- mailbox_state ----

    def get_mailbox_state(self, address: str) -> MailboxState | None:
        r = self.connection().execute("SELECT * FROM mailbox_state WHERE address=?", (address,)).fetchone()
        if r is None:
            return None
        return MailboxState(r["address"], json.loads(r["cursor"]), r["not_before"], r["scan_until"],
                            r["last_wide_scan"], r["first_run_at"])

    def init_mailbox_state(self, address: str, cursor: dict, not_before: str) -> None:
        """首次运行写入起点；已存在则不覆盖。"""
        with self._transaction() as c:
            c.execute("INSERT OR IGNORE INTO mailbox_state(address, cursor, not_before, first_run_at) VALUES (?,?,?,?)",
                      (address, json.dumps(cursor), not_before, _now()))

    def set_scan_until(self, address: str, value: str | None) -> None:
        with self._transaction() as c:
            c.execute("UPDATE mailbox_state SET scan_until=? WHERE address=?", (value, address))

    def set_last_wide_scan(self, address: str, value: str) -> None:
        with self._transaction() as c:
            c.execute("UPDATE mailbox_state SET last_wide_scan=? WHERE address=?", (value, address))

    def advance_cursor(self, address: str, cursor: dict) -> None:
        with self._transaction() as c:
            self._set_cursor(c, address, cursor)

    @staticmethod
    def _set_cursor(c: sqlite3.Connection, address: str, cursor: dict) -> None:
        if c.execute("UPDATE mailbox_state SET cursor=? WHERE address=?", (json.dumps(cursor), address)).rowcount != 1:
            raise LookupError(f"mailbox_state missing for {address}")

    # ---- processed ----

    def is_processed(self, address: str, mail_id: str) -> bool:
        return self.connection().execute("SELECT 1 FROM processed WHERE address=? AND mail_id=?",
                                         (address, mail_id)).fetchone() is not None

    def record_processed_and_advance(self, row: ProcessedRow, cursor: dict) -> None:
        """记录一封邮件的结果并推进游标，同一事务（设计 §3）。"""
        with self._transaction() as c:
            c.execute(
                "INSERT OR REPLACE INTO processed(address, mail_id, tenant, status, reason, subject, hits, attachments,"
                " location, at, skipped, original) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row.address, row.mail_id, row.tenant, row.status, row.reason, row.subject, row.hits,
                 row.attachments, row.location, row.at, row.skipped, row.original))
            self._set_cursor(c, row.address, cursor)

    def recent(self, tenant: str, limit: int = RECENT_LIMIT) -> list[ProcessedRow]:
        rows = self.connection().execute(
            "SELECT * FROM processed WHERE tenant=? ORDER BY at DESC, mail_id DESC LIMIT ?", (tenant, limit)).fetchall()
        return [ProcessedRow(r["address"], r["mail_id"], r["tenant"], r["status"], r["reason"], r["subject"],
                             r["hits"], r["attachments"], r["location"], r["at"], r["skipped"], r["original"]) for r in rows]

    def purge_processed_before(self, at: str) -> int:
        with self._transaction() as c:
            return c.execute("DELETE FROM processed WHERE at < ?", (at,)).rowcount

    # ---- rules ----

    def get_rules(self, tenant: str) -> tuple[tuple[str, str], ...]:
        rows = self.connection().execute("SELECT source, target FROM rules WHERE tenant=? ORDER BY position", (tenant,))
        return tuple((r["source"], r["target"]) for r in rows)

    def replace_rules(self, tenant: str, items: list[tuple[str, str]]) -> None:
        with self._transaction() as c:
            c.execute("DELETE FROM rules WHERE tenant=?", (tenant,))
            c.executemany("INSERT INTO rules(tenant, position, source, target) VALUES (?,?,?,?)",
                          [(tenant, i, s, t) for i, (s, t) in enumerate(items)])

    # ---- oauth_token ----

    def get_token(self, address: str) -> TokenRow | None:
        r = self.connection().execute("SELECT * FROM oauth_token WHERE address=?", (address,)).fetchone()
        if r is None:
            return None
        return TokenRow(r["address"], r["provider"], r["account"], r["refresh_token"], r["granted_at"],
                        r["status"], r["auth_generation"])

    def save_token(self, address: str, provider: str, account: str, refresh_token: str) -> None:
        with self._transaction() as c:
            gen = self._generation(c, address)
            c.execute(
                "INSERT OR REPLACE INTO oauth_token(address, provider, account, refresh_token, granted_at, status, auth_generation)"
                " VALUES (?,?,?,?,?,'ok',?)", (address, provider, account, refresh_token, _now(), gen))

    def rotate_refresh_token(self, address: str, new_token: str | None) -> None:
        """刷新后换发了新 refresh token 才替换；响应里没有就保留原值（E25）。"""
        if new_token is None:
            return
        with self._transaction() as c:
            c.execute("UPDATE oauth_token SET refresh_token=? WHERE address=?", (new_token, address))

    def mark_expired(self, address: str) -> None:
        with self._transaction() as c:
            c.execute("UPDATE oauth_token SET status='expired' WHERE address=?", (address,))

    def delete_token(self, address: str) -> None:
        with self._transaction() as c:
            c.execute("DELETE FROM oauth_token WHERE address=?", (address,))

    def get_generation(self, address: str) -> int:
        return self._generation(self.connection(), address)

    def bump_generation(self, address: str) -> int:
        """Disconnect 时递增；在途授权流程完成前核对（E32）。"""
        with self._transaction() as c:
            gen = self._generation(c, address) + 1
            c.execute("INSERT OR REPLACE INTO auth_generation(address, generation) VALUES (?,?)", (address, gen))
            c.execute("UPDATE oauth_token SET auth_generation=? WHERE address=?", (gen, address))
            return gen

    @staticmethod
    def _generation(c: sqlite3.Connection, address: str) -> int:
        r = c.execute("SELECT generation FROM auth_generation WHERE address=?", (address,)).fetchone()
        return r[0] if r else 0

    # ---- audit ----

    def audit(self, action: str, target: str, client: str = "") -> None:
        with self._transaction() as c:
            c.execute("INSERT INTO audit(at, action, target, client) VALUES (?,?,?,?)", (_now(), action, target, client))

    def recent_audit(self, limit: int = RECENT_LIMIT) -> list[AuditRow]:
        rows = self.connection().execute("SELECT * FROM audit ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [AuditRow(r["at"], r["action"], r["target"], r["client"]) for r in rows]
