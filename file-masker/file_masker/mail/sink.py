"""输出：`FsSink` 按 6617 契约落盘（原子写、幂等），`SmtpSink` 新建邮件投递（演示）。以后可加 `S3Sink`（设计 D11）。
`OriginalStore` 存没能完整打码的邮件原件，目录与落盘目录分开、只给服务自己读。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import smtplib
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from file_masker.mail.config import Mailbox
from file_masker.mail.mime import MaskedMail, build

MAX_STEM_BYTES = 100
DIR_MODE = 0o750
FILE_MODE = 0o640
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


@dataclass(frozen=True)
class MailContext:
    tenant: str
    source: str
    provider: str
    mail_id: str
    received_at: datetime      # 来源时间戳，决定日期目录（E17）
    rules_version: str


@dataclass(frozen=True)
class Delivered:
    location: str
    subject: str
    hits: int
    attachments: int
    reused: bool = False       # 命中已有 .ready：元数据来自已有 mail.json（E7）


class DirConflict(Exception):
    """目标目录已有 .ready 但不是这封邮件（哈希碰撞或内容损坏，E28）。"""


class PermanentSendError(Exception):
    """邮件服务器以 5xx 拒收，重试无用；调用方扣留后处理下一封。"""


class Sink(Protocol):
    def deliver(self, mail: MaskedMail, ctx: MailContext) -> Delivered: ...


def dir_id(provider: str, mail_id: str) -> str:
    return hashlib.sha256(provider.encode() + b"\x00" + mail_id.encode()).hexdigest()[:32]


def safe_filename(name: str, used: set[str]) -> str:
    """附件名清洗（E15）：控制字符丢弃；路径分隔符切段、丢掉 . 与 .. 段、用 _ 拼回（a/b 与 ab 不撞名）；
    NFC；主干按 UTF-8 字节截断；目录内按 casefold 去重。"""
    name = unicodedata.normalize("NFC", name or "")
    name = "".join(ch for ch in name if ord(ch) >= 0x20 and ord(ch) != 0x7F)
    segments = [seg for seg in name.replace("\\", "/").split("/") if seg.strip(" ") not in ("", ".", "..")]
    name = "_".join(seg.strip(" ") for seg in segments)
    stem, dot, ext = name.rpartition(".")
    if not dot or not ext or " " in ext or len(ext) > 10:
        stem, ext = name, "pdf"
    stem = stem.strip(" .") or "attachment"
    stem = stem.encode("utf-8")[:MAX_STEM_BYTES].decode("utf-8", "ignore").rstrip(" .") or "attachment"
    candidate, n = f"{stem}.{ext}", 1
    while candidate.casefold() in used:
        candidate = f"{stem} ({n}).{ext}"
        n += 1
    used.add(candidate.casefold())
    return candidate


def _mkdirs(path: Path, mode: int = DIR_MODE) -> None:
    """逐级建目录并显式 chmod：mkdir(parents=True) 只对最后一级用 mode，中间层会跟着 umask 走。"""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for p in reversed(missing):
        p.mkdir(mode=mode)
        os.chmod(p, mode)


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _fingerprint(mail: MaskedMail, received_at: str) -> str:
    if mail.message_id:
        basis = mail.message_id
    else:
        sizes = ",".join(str(len(data)) for _, data in mail.attachments)
        basis = f"{received_at}|{mail.subject}|{len(mail.attachments)}|{sizes}"
    return "sha256:" + hashlib.sha256(basis.encode()).hexdigest()


class OriginalStore:
    """整封原件存为 `<根>/<租户>/<邮箱>/<来源日期>/<dir_id>.eml`，旁边 `<dir_id>.json` 记原因。
    重复保存直接覆盖（同一封邮件内容不变）；目录 700、文件 600。"""

    def __init__(self, root: str | Path, timezone: str) -> None:
        self._root = Path(root)
        self._tz = ZoneInfo(timezone)

    def save(self, raw: bytes, ctx: MailContext, reason: str, skipped: list[tuple[str, str]],
             removed_images: int = 0) -> str:
        day = self._root / ctx.tenant / ctx.source / ctx.received_at.astimezone(self._tz).date().isoformat()
        _mkdirs(day, PRIVATE_DIR_MODE)
        name = dir_id(ctx.provider, ctx.mail_id)
        meta = {"tenant": ctx.tenant, "source": ctx.source, "mail_id": ctx.mail_id,
                "received_at": ctx.received_at.isoformat(), "reason": reason,
                "skipped": [{"name": n, "reason": r} for n, r in skipped], "removed_images": removed_images}
        self._replace(day / f"{name}.json", json.dumps(meta, ensure_ascii=False, indent=2).encode())
        self._replace(day / f"{name}.eml", raw)
        _fsync_path(day)
        return str(day / f"{name}.eml")

    @staticmethod
    def _replace(path: Path, data: bytes) -> None:
        tmp = path.with_name(f"{path.name}.tmp-{secrets.token_hex(4)}")
        try:
            _write_new(tmp, data, PRIVATE_FILE_MODE)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


class FsSink:
    def __init__(self, output_root: str | Path, timezone: str, version: str) -> None:
        self._root = Path(output_root)
        self._tz = ZoneInfo(timezone)
        self._version = version

    def deliver(self, mail: MaskedMail, ctx: MailContext) -> Delivered:
        day = ctx.received_at.astimezone(self._tz).date().isoformat()
        final = self._root / ctx.tenant / ctx.source / day / dir_id(ctx.provider, ctx.mail_id)
        if (final / ".ready").exists():
            return self._reuse(final, ctx)
        _mkdirs(final.parent)
        tmp = final.parent / f"{final.name}.tmp-{secrets.token_hex(4)}"
        tmp.mkdir(mode=DIR_MODE)
        try:
            names = self._write_attachments(tmp, mail)
            received = ctx.received_at.isoformat()
            meta = {
                "tenant": ctx.tenant, "source": ctx.source, "provider": ctx.provider,
                "mail_id": ctx.mail_id, "message_id": mail.message_id,
                "dedup_fingerprint": _fingerprint(mail, received),
                "original_date": mail.original_datetime.isoformat() if mail.original_datetime else None,
                "received_at": received,
                "from_alias": mail.original_from, "subject": mail.subject,
                "body_text": mail.text_body, "body_html": mail.html_body,
                "attachments": names, "removed_images": mail.removed_images, "hits": mail.hits,
                "skipped": [{"name": name, "reason": reason} for name, reason in mail.skipped],
                "masked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "rules_version": ctx.rules_version, "masker_version": self._version,
            }
            _write_new(tmp / "mail.json", json.dumps(meta, ensure_ascii=False, indent=2).encode())
            _fsync_path(tmp)
            if final.exists():  # 上次写了一半（无 .ready）
                shutil.rmtree(final)
            os.rename(tmp, final)
            _fsync_path(final.parent)
            _write_new(final / ".ready", b"")
            _fsync_path(final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return Delivered(str(final), mail.subject, mail.hits, len(names))

    def _write_attachments(self, tmp: Path, mail: MaskedMail) -> list[str]:
        used: set[str] = set()
        names = []
        root = tmp.resolve()
        for name, data in mail.attachments:
            safe = safe_filename(name, used)
            path = tmp / safe
            if path.resolve().parent != root:  # 清洗后仍不在目录内：不写
                raise DirConflict(f"attachment path escapes mail directory: {safe!r}")
            _write_new(path, data)
            names.append(safe)
        return names

    @staticmethod
    def _reuse(final: Path, ctx: MailContext) -> Delivered:
        try:
            meta = json.loads((final / "mail.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise DirConflict(f"{final}: mail.json unreadable") from e
        if meta.get("mail_id") != ctx.mail_id:
            raise DirConflict(f"{final}: holds mail {meta.get('mail_id')!r}, not {ctx.mail_id!r}")
        return Delivered(str(final), str(meta.get("subject", "")), int(meta.get("hits", 0)),
                         len(meta.get("attachments") or []), reused=True)


class SmtpSink:
    TIMEOUT_S = 30

    def __init__(self, mailbox: Mailbox, version: str) -> None:
        self._box = mailbox
        self._version = version

    def deliver(self, mail: MaskedMail, ctx: MailContext) -> Delivered:
        msg = build(mail, self._box.address, self._box.smtp_to, self._version)
        with smtplib.SMTP_SSL(self._box.smtp_host, self._box.smtp_port, timeout=self.TIMEOUT_S) as smtp:
            smtp.login(self._box.address, self._box.password)  # 登录失败是配置问题，不算拒收
            try:
                smtp.send_message(msg)
            except smtplib.SMTPRecipientsRefused as e:
                if all(code >= 500 for code, _ in e.recipients.values()):
                    raise PermanentSendError(str(e)) from e
                raise
            except smtplib.SMTPResponseException as e:
                if e.smtp_code >= 500:
                    raise PermanentSendError(f"{e.smtp_code} {e.smtp_error!r}") from e
                raise
        return Delivered(self._box.smtp_to, mail.subject, mail.hits, len(mail.attachments))
