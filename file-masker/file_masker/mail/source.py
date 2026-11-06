"""取件后端的统一接口。三种后端（IMAP / Gmail API / Graph）都返回整封原始邮件，后面的打码与落盘完全一样。

去重与进度只认本地 SQLite（设计 D7）：候选允许包含已处理的邮件；游标只在同一 epoch 内单调前进。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


@dataclass(frozen=True)
class Candidate:
    mail_id: str            # 来源内唯一的原始 id
    ts: datetime            # 来源时间戳：IMAP INTERNALDATE / Gmail internalDate / Graph receivedDateTime
    cursor: dict            # 处理到这封后应推进到的游标
    raw: bytes | None = field(default=None, repr=False)  # 发现阶段已取到整封时顺带携带（Gmail）


@dataclass(frozen=True)
class Scan:
    candidates: list[Candidate]        # 已按 (ts, mail_id) 或 UID 升序
    cursor_after: dict                 # 本次扫描完整处理后应到达的游标（UIDVALIDITY 重置时即新基线）
    scan_until: datetime | None = None


class MailSource(Protocol):
    def open(self) -> None: ...
    def initial_cursor(self, since: date | None) -> dict: ...
    def cursor_time(self, cursor: dict) -> datetime | None: ...   # 时间型游标返回时间，worker 据此算窗口；IMAP 返回 None
    def scan(self, cursor: dict, window: tuple[datetime, datetime]) -> Scan: ...
    def fetch(self, mail_id: str) -> bytes: ...
    def mark_done(self, mail_id: str) -> None: ...   # 服务端标记，只给人看，尽力而为
    def close(self) -> None: ...
