"""后台轮询（设计 §5.2）：每个邮箱 取件 → 整封打码 → 落盘 / 发信 → 本地记已处理 → 服务端标记。
进度与去重只认 SQLite；一个邮箱失败不影响其他邮箱；OAuth 邮箱无凭据或已失效则跳过。"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from file_masker.mail.config import MailConfig, Mailbox
from file_masker.mail.httpclient import Http, Unauthorized
from file_masker.mail.masker import Held, mask_mail
from file_masker.mail.mime import is_own_output
from file_masker.mail.oauth2 import InvalidGrant, Provider, Token, refresh
from file_masker.mail.rules import TenantRules
from file_masker.mail.sink import DirConflict, MailContext, OriginalStore, PermanentSendError, Sink
from file_masker.mail.source import Candidate, MailSource, Scan
from file_masker.mail.state import ProcessedRow, StateDb

log = logging.getLogger("file_masker.mail")

OAUTH_PROVIDER = {"gmail": "google", "graph": "microsoft"}   # 取件后端 → OAuth 提供方
BACKFILL_SLICE = timedelta(days=1)
WIDE_RESCAN = timedelta(hours=24)
TOKEN_SLACK_S = 60
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TMP_DIR = re.compile(r"\.tmp-[0-9a-f]+$")


class LockHeld(Exception):
    """另一个实例已持有 /data/lock（E27）。"""


class NotAuthorized(Exception):
    """OAuth 邮箱还没有授权。"""


class AuthExpired(Exception):
    """refresh token 已失效，需要重新授权。"""


def acquire_lock(path: str | Path):
    """独占文件锁，返回的文件对象要一直持有到进程退出。"""
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise LockHeld(f"{path} is held by another instance") from None
    return f


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------- OAuth access token ----------

class TokenManager:
    """按邮箱缓存 access token，快过期时用库里的 refresh token 换新；`invalid_grant` 把邮箱标为 expired。"""

    def __init__(self, state: StateDb, providers: dict[str, Provider], http: Http) -> None:
        self._state = state
        self._providers = providers
        self.http = http
        self._cache: dict[str, Token] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, address: str) -> threading.Lock:
        with self._locks_guard:  # 每邮箱一把锁：一家 token 端点卡住不影响其他邮箱与 Disconnect
            return self._locks.setdefault(address, threading.Lock())

    def auth_state(self, address: str) -> str:
        row = self._state.get_token(address)
        if row is None:
            return "not_authorized"
        return "expired" if row.status == "expired" else "authorized"

    def access_token(self, address: str, provider_name: str, force: bool = False) -> str:
        with self._lock(address):
            row = self._state.get_token(address)
            if row is None:
                self._cache.pop(address, None)
                raise NotAuthorized(address)
            if row.status == "expired":
                self._cache.pop(address, None)
                raise AuthExpired(address)
            cached = self._cache.get(address)
            if cached and not force and not cached.expires_within(TOKEN_SLACK_S):
                return cached.access_token
            try:
                token = refresh(self._providers[provider_name], self.http, row.refresh_token)
            except InvalidGrant:
                log.warning("refresh token for %s rejected (invalid_grant); re-authorization needed", address)
                self._state.mark_expired(address)
                self._cache.pop(address, None)
                raise AuthExpired(address) from None
            self._state.rotate_refresh_token(address, token.refresh_token)
            self._cache[address] = token
            return token.access_token

    def forget(self, address: str) -> None:
        with self._lock(address):
            self._cache.pop(address, None)


# ---------- 状态 ----------

@dataclass(frozen=True)
class MailboxStatus:
    last_poll_at: str | None = None
    last_error: str | None = None
    auth_state: str = "password"      # password / not_authorized / authorized / expired
    backfill: str | None = None       # 回溯中：scan_until


class _Ctx:
    """一个邮箱一轮内的工作状态。"""

    def __init__(self, box: Mailbox, source: MailSource, cursor: dict, not_before: datetime) -> None:
        self.box, self.source, self.cursor, self.not_before = box, source, cursor, not_before
        self.handled = 0
        self.capped = False


class MailWorker:
    def __init__(self, config: MailConfig, state: StateDb, rules: TenantRules, sources: dict[str, MailSource],
                 sinks: dict[str, Sink], tokens: TokenManager | None, version: str,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._config = config
        self._state = state
        self._rules = rules
        self._sources = sources
        self._sinks = sinks
        self._originals = OriginalStore(config.run.originals_root, config.run.timezone) if config.run.originals_root else None
        self._tokens = tokens
        self._version = version
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._tz = ZoneInfo(config.run.timezone)
        self._status: dict[str, MailboxStatus] = {b.address: MailboxStatus() for b in config.mailboxes()}
        self._last_cleanup_day: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        os.umask(0o027)
        self._thread = threading.Thread(target=self._run, name="mail-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._config.run.poll_seconds)

    # ---- 状态查询 ----

    def status(self, address: str) -> MailboxStatus:
        st = self._status[address]
        ms = self._state.get_mailbox_state(address)
        box = self._config.mailbox(address)
        auth = "password"
        if box.provider in OAUTH_PROVIDER and self._tokens is not None:
            auth = self._tokens.auth_state(address)
        return MailboxStatus(st.last_poll_at, st.last_error, auth, ms.scan_until if ms else None)

    # ---- 轮询 ----

    def poll_once(self) -> None:
        for box in self._config.mailboxes():
            self._poll_mailbox(box)
        self._maybe_cleanup()

    def _poll_mailbox(self, box: Mailbox) -> None:
        error = None
        polled = False
        try:
            if not self._ensure_auth(box):
                return
            polled = True
            try:
                self._poll_source(box)
            except Unauthorized:
                # access token 被拒：强制刷新后整轮重试一次；再 401 记 api_unauthorized，不动持久状态（E12）
                log.warning("%s: API rejected access token, refreshing once", box.address)
                self._tokens.access_token(box.address, OAUTH_PROVIDER[box.provider], force=True)
                try:
                    self._poll_source(box)
                except Unauthorized:
                    error = "api_unauthorized"
        except (NotAuthorized, AuthExpired):
            return  # 页面从 auth_state 看得到，不算错误
        except Exception as e:  # 本轮放弃，下一轮重试；未记已处理的邮件会重新处理
            log.exception("%s: poll failed", box.address)
            error = f"{type(e).__name__}: {e}"
        finally:
            if polled or error:
                self._status[box.address] = MailboxStatus(_now_text(), error)

    def _ensure_auth(self, box: Mailbox) -> bool:
        if box.provider not in OAUTH_PROVIDER or self._tokens is None:
            return True
        try:
            self._tokens.access_token(box.address, OAUTH_PROVIDER[box.provider])
            return True
        except (NotAuthorized, AuthExpired):
            return False  # 没取件，状态里的「上次检查」时间不动

    def _poll_source(self, box: Mailbox) -> None:
        source = self._sources[box.address]
        try:
            source.open()
            ms = self._state.get_mailbox_state(box.address)
            if ms is None:
                self._initialize(box, source)
                return
            ctx = _Ctx(box, source, ms.cursor, datetime.fromisoformat(ms.not_before))
            now = self.clock()
            timed = source.cursor_time(ms.cursor) is not None
            if timed and ms.scan_until:
                self._backfill_slice(ctx, datetime.fromisoformat(ms.scan_until), now)
                return
            cursor_time = source.cursor_time(ms.cursor) if timed else now
            self._scan(ctx, (cursor_time - timedelta(minutes=self._config.run.lookback_minutes), now))
            if timed and not ctx.capped and self._wide_rescan_due(ms.last_wide_scan, now):
                self._scan(ctx, (now - WIDE_RESCAN, now))
                if not ctx.capped:
                    self._state.set_last_wide_scan(box.address, now.isoformat())
        finally:
            source.close()

    def _initialize(self, box: Mailbox, source: MailSource) -> None:
        """首次运行：只记起点，不取件（设计 §4.2、E1）。"""
        now = self.clock()
        cursor = source.initial_cursor(box.since)
        since_start = datetime(box.since.year, box.since.month, box.since.day, tzinfo=timezone.utc) if box.since else None
        not_before = since_start or now
        self._state.init_mailbox_state(box.address, cursor, not_before.isoformat())
        self._state.set_last_wide_scan(box.address, now.isoformat())
        if since_start and source.cursor_time(cursor) is not None:
            self._state.set_scan_until(box.address, source.cursor_time(cursor).isoformat())
        log.info("%s: initialized, since=%s", box.address, box.since)

    def _backfill_slice(self, ctx: _Ctx, scan_until: datetime, now: datetime) -> None:
        """回溯（E30）：按天推进 scan_until；最后一片到 now 为止，扫完置 NULL。"""
        end = scan_until + BACKFILL_SLICE
        last = end >= now
        if last:
            end = now
        self._scan(ctx, (scan_until, end))
        if ctx.capped:
            return  # 本片未扫完，下一轮同一片继续（已处理的会被跳过）
        self._state.set_scan_until(ctx.box.address, None if last else end.isoformat())

    def _wide_rescan_due(self, last_wide_scan: str | None, now: datetime) -> bool:
        if not last_wide_scan:
            return True
        return datetime.fromisoformat(last_wide_scan).astimezone(self._tz).date() < now.astimezone(self._tz).date()

    def _scan(self, ctx: _Ctx, window: tuple[datetime, datetime]) -> None:
        scan: Scan = ctx.source.scan(ctx.cursor, window)
        all_handled = True
        for cand in scan.candidates:
            if cand.ts < ctx.not_before or self._state.is_processed(ctx.box.address, cand.mail_id):
                ctx.cursor = self._later(ctx.source, ctx.cursor, cand.cursor)
                continue
            if ctx.handled >= self._config.run.max_per_poll:
                all_handled = False
                break
            self._process(ctx, cand)
            ctx.handled += 1
        if all_handled:
            ctx.cursor = self._later(ctx.source, ctx.cursor, scan.cursor_after)
        else:
            ctx.capped = True
        self._state.advance_cursor(ctx.box.address, ctx.cursor)

    def _process(self, ctx: _Ctx, cand: Candidate) -> None:
        box = ctx.box
        raw = cand.raw if cand.raw is not None else ctx.source.fetch(cand.mail_id)
        row = self._handle(box, cand, raw)
        ctx.cursor = self._later(ctx.source, ctx.cursor, cand.cursor)
        self._state.record_processed_and_advance(row, ctx.cursor)  # 先记已处理再标记：标记失败也不会重做
        try:
            ctx.source.mark_done(cand.mail_id)
        except Exception as e:
            log.warning("%s: mark_done failed for %s: %s", box.address, cand.mail_id, e)
        if row.status == "held":
            log.warning("%s: mail %s held reason=%s original=%s", box.address, cand.mail_id, row.reason, bool(row.original))
        else:
            log.info("%s: mail %s %s hits=%s attachments=%s skipped=%s", box.address, cand.mail_id, row.status, row.hits,
                     row.attachments, row.skipped)

    def _handle(self, box: Mailbox, cand: Candidate, raw: bytes) -> ProcessedRow:
        mctx = MailContext(box.tenant, box.address, box.provider, cand.mail_id, cand.ts, self._rules.version(box.tenant))

        def held(reason: str, subject: str = "", skipped: list[tuple[str, str]] = ()) -> ProcessedRow:
            original = self._save_original(raw, mctx, reason, list(skipped))
            return ProcessedRow(box.address, cand.mail_id, box.tenant, "held", reason, subject, 0, 0, "", _now_text(),
                                len(skipped), original)

        if is_own_output(raw):
            return ProcessedRow(box.address, cand.mail_id, box.tenant, "held", "own_output", "", 0, 0, "", _now_text())
        keywords = dict(self._rules.snapshot(box.tenant))  # 每封取一次快照，处理中途改规则只影响下一封
        if not keywords:
            return held("no_rules")
        try:
            result = mask_mail(raw, keywords)
        except Exception:
            log.exception("%s: mask failed for %s", box.address, cand.mail_id)
            return held("internal_error")
        if isinstance(result, Held):
            return held(result.reason, result.masked_subject)
        # 先存原件再投递：存失败就抛出，这封不记已处理，下一轮重来
        # 图片不打码直接去掉，原件也要留一份，不然本地就没有图了
        original = (self._save_original(raw, mctx, "", result.skipped, result.removed_images)
                    if result.skipped or result.removed_images else "")
        try:
            out = self._sinks[box.address].deliver(result, mctx)
        except DirConflict as e:
            log.error("%s: %s", box.address, e)
            return held("dir_conflict", result.subject, result.skipped)
        except PermanentSendError as e:
            log.warning("%s: mail %s rejected by server: %s", box.address, cand.mail_id, e)
            return held("send_rejected", result.subject, result.skipped)
        status = "sent" if box.sink == "smtp" else "written"
        return ProcessedRow(box.address, cand.mail_id, box.tenant, status, "", out.subject, out.hits, out.attachments,
                            out.location, _now_text(), len(result.skipped), original)

    def _save_original(self, raw: bytes, ctx: MailContext, reason: str, skipped: list[tuple[str, str]],
                       removed_images: int = 0) -> str:
        if self._originals is None:
            return ""
        return self._originals.save(raw, ctx, reason, skipped, removed_images)

    @staticmethod
    def _later(source: MailSource, a: dict, b: dict) -> dict:
        """两个游标取靠后的那个；回退窗口里的候选不会把游标往回拉。"""
        ta, tb = source.cursor_time(a), source.cursor_time(b)
        if ta is None or tb is None:  # UID 型（IMAP）：换了 UIDVALIDITY 就以新的为准
            if a.get("uidvalidity") != b.get("uidvalidity"):
                return b
            return b if int(b.get("last_uid", -1)) >= int(a.get("last_uid", -1)) else a
        return b if (tb, str(b.get("id", ""))) >= (ta, str(a.get("id", ""))) else a

    # ---- 清理 ----

    def _maybe_cleanup(self) -> None:
        today = self.clock().astimezone(self._tz).date().isoformat()
        if self._last_cleanup_day == today:
            return
        self._last_cleanup_day = today
        try:
            removed = cleanup(self._config.run.output_root, self._config.run.retain_days, self._state, self.clock(),
                              self._config.run.timezone, self._config.run.originals_root)
            log.info("cleanup: %s", removed)
        except Exception:
            log.exception("cleanup failed")


def cleanup(output_root: str | Path, retain_days: int, state: StateDb, now: datetime, timezone_name: str,
            originals_root: str | Path = "") -> dict:
    """删除早于 retain_days 的日期目录、残留的 *.tmp-* 与 processed 行（设计 §5.3）。只进「租户/来源/日期」三层。
    原件目录同样按日期清理，残留的是 *.tmp-* 文件。"""
    root = Path(output_root)
    if not root.is_absolute() or len(root.parts) < 3:  # E23：`/`、`/srv`、`/Users` 这类顶层目录拒绝
        raise ValueError(f"unsafe output_root for cleanup: {output_root}")
    local_now = now.astimezone(ZoneInfo(timezone_name))
    cutoff_day = (local_now - timedelta(days=retain_days)).date().isoformat()
    counts = {"day_dirs": 0, "tmp_dirs": 0, "processed_rows": 0}
    for day_dir in _day_dirs(root):
        if day_dir.name < cutoff_day:
            shutil.rmtree(day_dir)
            counts["day_dirs"] += 1
            continue
        for d in _subdirs(day_dir):
            if _TMP_DIR.search(d.name):
                shutil.rmtree(d)
                counts["tmp_dirs"] += 1
    for day_dir in _day_dirs(Path(originals_root)) if originals_root else ():
        if day_dir.name < cutoff_day:
            shutil.rmtree(day_dir)
            counts["day_dirs"] += 1
            continue
        for f in day_dir.iterdir():
            if f.is_file() and _TMP_DIR.search(f.name):
                f.unlink()
                counts["tmp_dirs"] += 1
    # processed.at 由 _now_text() 用进程本地时区写入，截止时间必须同一时区才能按字符串比较
    cutoff_at = (now - timedelta(days=retain_days)).astimezone().isoformat(timespec="seconds")
    counts["processed_rows"] = state.purge_processed_before(cutoff_at)
    return counts


def _day_dirs(root: Path):
    """「租户/来源/日期」第三层里名字是日期的目录。"""
    if not root.is_dir():
        return
    for tenant_dir in _subdirs(root):
        for source_dir in _subdirs(tenant_dir):
            for day_dir in _subdirs(source_dir):
                if _DAY_DIR.match(day_dir.name):
                    yield day_dir


def _subdirs(path: Path) -> list[Path]:
    return [p for p in sorted(path.iterdir()) if p.is_dir() and not p.is_symlink()]
