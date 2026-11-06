import fcntl
import json
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

import fitz
import pytest

from conftest import make_pdf
from file_masker.mail.config import Mailbox, MailConfig, RunConfig, Tenant
from file_masker.mail.httpclient import HttpError, Unauthorized
from file_masker.mail.oauth2 import google_provider
from file_masker.mail.rules import TenantRules
from file_masker.mail.sink import Delivered, DirConflict, MailContext, PermanentSendError
from file_masker.mail.source import Candidate, Scan
from file_masker.mail.state import StateDb
from file_masker.mail.worker import AuthExpired, LockHeld, MailWorker, TokenManager, acquire_lock

T0 = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
PDF = make_pdf(["Client Zhang San"])


def mailbox(address="a@x.com", tenant="lq", provider="gmail", sink="fs", since=None):
    return Mailbox(tenant=tenant, address=address, provider=provider, sink=sink, password="pw" if provider == "imap" else None,
                   imap_host="imap.x", imap_port=993, smtp_host="smtp.x", smtp_port=465,
                   smtp_to="dst@x.com" if sink == "smtp" else None, since=since)


def config(*boxes, max_per_poll=50, lookback=10, originals_root=""):
    tenants = {}
    for b in boxes:
        tenants.setdefault(b.tenant, []).append(b)
    return MailConfig(
        run=RunConfig(poll_seconds=60, output_root="/srv/masked", retain_days=90, timezone="Asia/Shanghai",
                      max_per_poll=max_per_poll, lookback_minutes=lookback, originals_root=originals_root),
        google=None, microsoft=None,
        tenants=tuple(Tenant(code, tuple(bs)) for code, bs in tenants.items()),
    )


def raw_mail(subject, pdf=None, headers=None):
    msg = EmailMessage()
    msg["From"] = "rm@bank.com"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 16 Oct 2026 09:00:00 +0000"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content("body")
    if pdf:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="a.pdf")
    return msg.as_bytes()


class FakeSource:
    """时间型游标（像 Gmail）：mails = {id: (ts, raw)}；scan 返回窗口内、未处理的候选。"""

    def __init__(self, mails=None, is_processed=lambda _: False, carry_raw=True):
        self.mails = dict(mails or {})
        self.is_processed = is_processed
        self.carry_raw = carry_raw
        self.scans, self.fetched, self.done, self.fail_open, self.fail_scan_401 = [], [], [], None, 0
        self.opened = self.closed = 0

    def open(self):
        self.opened += 1
        if self.fail_open:
            raise self.fail_open

    def close(self):
        self.closed += 1

    def initial_cursor(self, since):
        start = datetime(since.year, since.month, since.day, tzinfo=timezone.utc) if since else T0
        return {"ts": start.isoformat(), "id": ""}

    @staticmethod
    def cursor_time(cursor):
        return datetime.fromisoformat(cursor["ts"])

    def scan(self, cursor, window):
        self.scans.append(window)
        if self.fail_scan_401:
            self.fail_scan_401 -= 1
            raise Unauthorized("scan")
        start, end = window
        cands = [Candidate(mid, ts, {"ts": ts.isoformat(), "id": mid}, raw if self.carry_raw else None)
                 for mid, (ts, raw) in self.mails.items() if start <= ts < end and not self.is_processed(mid)]
        cands.sort(key=lambda c: (c.ts, c.mail_id))
        return Scan(cands, {"ts": end.isoformat(), "id": ""})

    def fetch(self, mail_id):
        self.fetched.append(mail_id)
        return self.mails[mail_id][1]

    def mark_done(self, mail_id):
        if mail_id == "mark-fails":
            raise RuntimeError("label failed")
        self.done.append(mail_id)


class FakeSink:
    def __init__(self):
        self.delivered, self.fail_with = [], {}

    def deliver(self, mail, ctx):
        if ctx.mail_id in self.fail_with:
            raise self.fail_with[ctx.mail_id]
        self.delivered.append((mail, ctx))
        return Delivered(f"/srv/masked/{ctx.tenant}/{ctx.mail_id}", mail.subject, mail.hits, len(mail.attachments))


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    state = StateDb(tmp_path / "state.db")
    rules = TenantRules(state)
    rules.replace("lq", [{"source": "Zhang San", "target": "CLIENT-A"}])
    return state, rules


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post_form(self, url, data, headers=None):
        self.calls.append(data)
        status, body = self.responses.pop(0)
        if status >= 400:
            raise HttpError(status, body)
        return body


OK_TOKEN = (200, {"access_token": "at", "expires_in": 3600})


def token_manager(state, *responses):
    return TokenManager(state, {"google": google_provider("cid", "sec")}, FakeHttp(responses))


def make_worker(env, boxes, sources, sinks, tokens=None, clock=None, **cfg):
    state, rules = env
    if tokens is None:  # 默认：OAuth 邮箱都已授权
        tokens = token_manager(state, *[OK_TOKEN] * 10)
        for b in boxes:
            if b.provider != "imap" and state.get_token(b.address) is None:
                state.save_token(b.address, "google", b.address, "rt")
    return MailWorker(config(*boxes, **cfg), state, rules, sources, sinks, tokens, "1.3.0", clock=clock or Clock())


# ---------- 核心循环 ----------

def test_first_run_initializes_state_and_processes_only_new_mail(env):
    state, _ = env
    old = (T0 - timedelta(minutes=5), raw_mail("Zhang San old"))      # 启动前到达：回退窗口会列出，但 not_before 之前 → 跳过
    new = (T0 + timedelta(seconds=30), raw_mail("Zhang San new", PDF))
    src, sink = FakeSource({"old": old, "new": new}), FakeSink()
    box = mailbox()
    w = make_worker(env, [box], {box.address: src}, {box.address: sink})
    w.poll_once()                       # 首轮：只记起点
    assert state.get_mailbox_state("a@x.com").cursor == {"ts": T0.isoformat(), "id": ""}
    assert sink.delivered == [] and src.scans == []
    w.clock.now = T0 + timedelta(minutes=1)
    w.poll_once()
    assert src.scans[-1] == (T0 - timedelta(minutes=10), T0 + timedelta(minutes=1))  # 回退 10 分钟
    [(mail, ctx)] = sink.delivered
    assert mail.subject == "CLIENT-A new" and ctx.tenant == "lq" and ctx.provider == "gmail" and ctx.mail_id == "new"
    assert ctx.received_at == new[0] and ctx.rules_version.startswith("sha256:")
    assert state.is_processed("a@x.com", "new") and not state.is_processed("a@x.com", "old")
    assert src.done == ["new"] and src.fetched == []  # raw 随候选携带，不再 fetch
    rec = state.recent("lq")[0]
    assert (rec.status, rec.subject, rec.hits, rec.attachments, rec.location) == ("written", "CLIENT-A new", 2, 1, "/srv/masked/lq/new")
    assert state.get_mailbox_state("a@x.com").cursor == {"ts": (T0 + timedelta(minutes=1)).isoformat(), "id": ""}
    st = w.status("a@x.com")
    assert st.last_error is None and st.last_poll_at and st.auth_state == "authorized"


def test_smtp_sink_records_sent(env):
    box = mailbox(provider="imap", sink="smtp", tenant="demo")
    env[1].replace("demo", [{"source": "Zhang San", "target": "X"}])
    src = FakeSource({"m": (T0 + timedelta(seconds=1), raw_mail("Zhang San"))}, carry_raw=False)
    w = make_worker(env, [box], {box.address: src}, {box.address: FakeSink()})
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1); w.poll_once()
    assert src.fetched == ["m"] and env[0].recent("demo")[0].status == "sent"
    assert w.status(box.address).auth_state == "password"


def test_no_rules_holds_without_delivery(env):
    state, rules = env
    rules.replace("lq", [])
    box, src, sink = mailbox(), FakeSource({"m": (T0 + timedelta(seconds=1), raw_mail("Zhang San"))}), FakeSink()
    w = make_worker(env, [box], {box.address: src}, {box.address: sink})
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1); w.poll_once()
    assert sink.delivered == [] and src.done == ["m"]
    rec = state.recent("lq")[0]
    assert (rec.status, rec.reason, rec.subject) == ("held", "no_rules", "")


def test_scanned_pdf_skipped_and_held_by_sink(env):
    state, _ = env
    doc = fitz.open(); page = doc.new_page()
    page.insert_image(page.rect, pixmap=fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), 0))
    mails = {"scan": (T0 + timedelta(seconds=1), raw_mail("Zhang San scan", doc.tobytes())),
             "conflict": (T0 + timedelta(seconds=2), raw_mail("Zhang San c")),
             "rejected": (T0 + timedelta(seconds=3), raw_mail("Zhang San r"))}
    box, src, sink = mailbox(), FakeSource(mails), FakeSink()
    sink.fail_with = {"conflict": DirConflict("x"), "rejected": PermanentSendError("552")}
    w = make_worker(env, [box], {box.address: src}, {box.address: sink})
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1); w.poll_once()
    by_id = {r.mail_id: r for r in state.recent("lq")}
    assert (by_id["scan"].status, by_id["scan"].skipped, by_id["scan"].attachments) == ("written", 1, 0)  # 扫描件跳过，邮件照常落盘
    assert (by_id["conflict"].reason, by_id["rejected"].reason) == ("dir_conflict", "send_rejected")
    assert sorted(src.done) == ["conflict", "rejected", "scan"]


def raw_with_xlsx(subject):
    msg = EmailMessage()
    msg["From"] = "rm@bank.com"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 16 Oct 2026 09:00:00 +0000"
    msg.set_content("body")
    msg.add_attachment(b"xlsx", maintype="application", subtype="vnd.ms-excel", filename="Zhang San.xlsx")
    return msg.as_bytes()


def run_two_polls(env, mails, tmp_path=None, sink=None):
    box, src, sink = mailbox(), FakeSource(mails), sink or FakeSink()
    extra = {"originals_root": str(tmp_path / "originals")} if tmp_path else {}
    w = make_worker(env, [box], {box.address: src}, {box.address: sink}, **extra)
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1); w.poll_once()
    return {r.mail_id: r for r in env[0].recent("lq")}, sink


def test_originals_saved_for_held_and_skipped_but_not_clean_mail(env, tmp_path):
    env[1].replace("lq", [{"source": "Zhang San", "target": "CLIENT-A"}])
    mails = {"skip": (T0 + timedelta(seconds=1), raw_with_xlsx("Zhang San skip")),
             "clean": (T0 + timedelta(seconds=2), raw_mail("Zhang San ok", PDF)),
             "undecodable": (T0 + timedelta(seconds=3), b"From: rm@bank.com\r\nSubject: =?utf-8?B?5byg5LiJ\r\n\r\nbody\r\n")}
    rows, sink = run_two_polls(env, mails, tmp_path)
    skip, clean, bad = rows["skip"], rows["clean"], rows["undecodable"]
    assert (skip.status, skip.skipped) == ("written", 1) and skip.original.endswith(".eml")
    assert open(skip.original, "rb").read() == mails["skip"][1]
    assert [ctx.mail_id for _, ctx in sink.delivered] == ["skip", "clean"]
    assert clean.original == "" and clean.skipped == 0
    assert (bad.status, bad.reason) == ("held", "decode_error") and bad.original.endswith(".eml")
    assert str(tmp_path / "originals" / "lq" / "a@x.com") in bad.original


def test_no_rules_original_saved(env, tmp_path):
    env[1].replace("lq", [])
    rows, _ = run_two_polls(env, {"m": (T0 + timedelta(seconds=1), raw_mail("Zhang San"))}, tmp_path)
    assert rows["m"].status == "held" and rows["m"].original.endswith(".eml")


def test_original_saved_when_images_removed(env, tmp_path):
    env[1].replace("lq", [{"source": "Zhang San", "target": "CLIENT-A"}])
    msg = EmailMessage()
    msg["From"] = "rm@bank.com"
    msg["Subject"] = "Zhang San trade"
    msg["Date"] = "Fri, 16 Oct 2026 09:00:00 +0000"
    msg.set_content("see screenshot")
    msg.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="trade.png")
    rows, sink = run_two_polls(env, {"img": (T0 + timedelta(seconds=1), msg.as_bytes())}, tmp_path)
    row = rows["img"]
    assert (row.status, row.skipped) == ("written", 0) and row.original.endswith(".eml")
    assert open(row.original, "rb").read() == msg.as_bytes()
    assert json.loads(open(row.original[:-4] + ".json").read())["removed_images"] == 1


def test_without_originals_root_nothing_is_saved(env):
    rows, sink = run_two_polls(env, {"skip": (T0 + timedelta(seconds=1), raw_with_xlsx("Zhang San"))})
    assert (rows["skip"].status, rows["skip"].skipped, rows["skip"].original) == ("written", 1, "")


def test_original_save_failure_retries_mail(env, tmp_path):
    (tmp_path / "originals").write_text("not a dir")  # 存不了原件：这封不记已处理，下一轮重来
    rows, sink = run_two_polls(env, {"skip": (T0 + timedelta(seconds=1), raw_with_xlsx("Zhang San"))}, tmp_path)
    assert rows == {} and sink.delivered == []


def test_mark_done_failure_is_only_logged(env, caplog):
    state, _ = env
    box, src = mailbox(), FakeSource({"mark-fails": (T0 + timedelta(seconds=1), raw_mail("Zhang San"))})
    w = make_worker(env, [box], {box.address: src}, {box.address: FakeSink()})
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1); w.poll_once()
    assert state.is_processed("a@x.com", "mark-fails") and w.status("a@x.com").last_error is None
    assert "label failed" in caplog.text


def test_reused_ready_dir_fills_record_from_existing_meta(env):
    state, _ = env
    box, src, sink = mailbox(), FakeSource({"m": (T0 + timedelta(seconds=1), raw_mail("Zhang San"))}), FakeSink()
    sink.deliver = lambda mail, ctx: Delivered("/srv/masked/lq/m", "OLD SUBJECT", 42, 3, reused=True)
    w = make_worker(env, [box], {box.address: src}, {box.address: sink})
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1); w.poll_once()
    rec = state.recent("lq")[0]
    assert (rec.subject, rec.hits, rec.attachments, rec.status) == ("OLD SUBJECT", 42, 3, "written")


def test_max_per_poll_counts_only_unprocessed_and_cursor_does_not_skip(env):
    state, _ = env
    mails = {f"m{i:02d}": (T0 + timedelta(seconds=1), raw_mail(f"Zhang San {i}")) for i in range(60)}  # 同一时间戳 60 封
    processed = set()
    box, src, sink = mailbox(), FakeSource(mails, is_processed=lambda m: m in processed), FakeSink()
    w = make_worker(env, [box], {box.address: src}, {box.address: sink}, max_per_poll=50)
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1); w.poll_once()
    assert len(sink.delivered) == 50
    processed.update(r.mail_id for r in state.recent("lq", limit=100))
    assert state.get_mailbox_state("a@x.com").cursor != {"ts": (T0 + timedelta(minutes=1)).isoformat(), "id": ""}  # 未处理完，不跳到窗口末
    w.poll_once()
    assert len(sink.delivered) == 60 and len({r.mail_id for r in state.recent("lq", limit=100)}) == 60
    assert state.get_mailbox_state("a@x.com").cursor == {"ts": (T0 + timedelta(minutes=1)).isoformat(), "id": ""}


def test_one_mailbox_failure_does_not_block_others(env):
    state, _ = env
    bad, good = mailbox("bad@x.com"), mailbox("good@x.com")
    sb, sg = FakeSource(), FakeSource({"m": (T0 + timedelta(seconds=1), raw_mail("Zhang San"))})
    sb.fail_open = OSError("connection refused")
    sink = FakeSink()
    w = make_worker(env, [bad, good], {"bad@x.com": sb, "good@x.com": sg}, {"bad@x.com": sink, "good@x.com": sink})
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1); w.poll_once()
    assert "connection refused" in w.status("bad@x.com").last_error and w.status("good@x.com").last_error is None
    assert len(sink.delivered) == 1 and sb.closed == sb.opened


# ---------- OAuth 状态 ----------

def test_oauth_mailbox_without_token_is_skipped(env):
    state, _ = env
    box, src = mailbox(), FakeSource()
    w = make_worker(env, [box], {box.address: src}, {box.address: FakeSink()}, tokens=token_manager(state))
    w.poll_once()
    assert w.status("a@x.com").auth_state == "not_authorized" and src.opened == 0
    assert w.status("a@x.com").last_poll_at is None   # 没取件就不显示「上次检查」


def test_oauth_refresh_and_expiry(env):
    state, _ = env
    state.save_token("a@x.com", "google", "a@x.com", "rt")
    box, src = mailbox(), FakeSource()
    tm = token_manager(state, (200, {"access_token": "at", "expires_in": 3600}), (400, {"error": "invalid_grant"}))
    w = make_worker(env, [box], {box.address: src}, {box.address: FakeSink()}, tokens=tm)
    w.poll_once()
    assert w.status("a@x.com").auth_state == "authorized" and tm.access_token("a@x.com", "google") == "at"
    assert tm.http.calls[0]["refresh_token"] == "rt" and state.get_token("a@x.com").refresh_token == "rt"
    with pytest.raises(AuthExpired):
        tm.access_token("a@x.com", "google", force=True)
    assert state.get_token("a@x.com").status == "expired"
    w.poll_once()
    assert w.status("a@x.com").auth_state == "expired" and src.opened == 1


def test_401_refreshes_once_then_records_api_unauthorized(env):
    state, _ = env
    state.save_token("a@x.com", "google", "a@x.com", "rt")
    box, src = mailbox(), FakeSource({"m": (T0 + timedelta(seconds=1), raw_mail("Zhang San"))})
    tm = token_manager(state, (200, {"access_token": "at1", "expires_in": 3600}), (200, {"access_token": "at2", "expires_in": 3600, "refresh_token": "rt2"}),
                       (200, {"access_token": "at3", "expires_in": 3600}))
    w = make_worker(env, [box], {box.address: src}, {box.address: FakeSink()}, tokens=tm)
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=1)
    src.fail_scan_401 = 1          # 第一次 401 → 刷新 → 重试成功
    w.poll_once()
    assert w.status("a@x.com").last_error is None and state.is_processed("a@x.com", "m")
    assert state.get_token("a@x.com").refresh_token == "rt2"  # 换发写回
    src.fail_scan_401 = 2          # 连续两次 401 → api_unauthorized，持久状态不变
    w.poll_once()
    assert w.status("a@x.com").last_error == "api_unauthorized" and state.get_token("a@x.com").status == "ok"


# ---------- 回溯、宽窗复扫 ----------

def test_since_backfill_advances_day_by_day_and_then_catches_up(env):
    state, _ = env
    since = date(2026, 10, 13)
    mails = {"d13": (datetime(2026, 10, 13, 8, tzinfo=timezone.utc), raw_mail("Zhang San 13")),
             "d14": (datetime(2026, 10, 14, 8, tzinfo=timezone.utc), raw_mail("Zhang San 14")),
             "live": (T0 + timedelta(seconds=1), raw_mail("Zhang San live"))}
    box, src, sink = mailbox(since=since), FakeSource(mails), FakeSink()
    w = make_worker(env, [box], {box.address: src}, {box.address: sink}, lookback=10)
    w.clock.now = T0 + timedelta(minutes=1)
    w.poll_once()  # 首轮：起点 = since，scan_until = since
    assert state.get_mailbox_state("a@x.com").scan_until == datetime(2026, 10, 13, tzinfo=timezone.utc).isoformat()
    w.poll_once()
    assert src.scans[-1] == (datetime(2026, 10, 13, tzinfo=timezone.utc), datetime(2026, 10, 14, tzinfo=timezone.utc))
    assert [m.subject for m, _ in sink.delivered] == ["CLIENT-A 13"]
    assert w.status("a@x.com").backfill == datetime(2026, 10, 14, tzinfo=timezone.utc).isoformat()
    w.poll_once(); w.poll_once()   # [14,15)、[15,16)
    assert [m.subject for m, _ in sink.delivered] == ["CLIENT-A 13", "CLIENT-A 14"]
    assert state.get_mailbox_state("a@x.com").scan_until == datetime(2026, 10, 16, tzinfo=timezone.utc).isoformat()
    w.poll_once()                  # [16, now]：追平
    assert src.scans[-1] == (datetime(2026, 10, 16, tzinfo=timezone.utc), T0 + timedelta(minutes=1))
    assert [m.subject for m, _ in sink.delivered] == ["CLIENT-A 13", "CLIENT-A 14", "CLIENT-A live"]
    assert state.get_mailbox_state("a@x.com").scan_until is None and w.status("a@x.com").backfill is None
    assert state.get_mailbox_state("a@x.com").cursor == {"ts": (T0 + timedelta(minutes=1)).isoformat(), "id": ""}
    w.clock.now = T0 + timedelta(minutes=2); w.poll_once()
    assert src.scans[-1] == (T0 + timedelta(minutes=1) - timedelta(minutes=10), T0 + timedelta(minutes=2))  # 回到常规窗口


def test_daily_wide_rescan_catches_late_mail(env):
    state, _ = env
    box, src, sink = mailbox(), FakeSource(), FakeSink()
    w = make_worker(env, [box], {box.address: src}, {box.address: sink})
    w.poll_once(); w.clock.now = T0 + timedelta(minutes=20); w.poll_once()   # 游标 → T0+20min
    # 一封时间戳 T0+5min 的邮件现在才出现：早于游标 − 10 分钟，常规窗口看不到
    src.mails["late"] = (T0 + timedelta(minutes=5), raw_mail("Zhang San late"))
    w.clock.now = T0 + timedelta(minutes=21); w.poll_once()
    assert sink.delivered == []
    w.clock.now = T0 + timedelta(days=1); w.poll_once()  # 新的一天：宽窗复扫 [now−24h, now]
    assert [m.subject for m, _ in sink.delivered] == ["CLIENT-A late"]
    assert any(w[0] == T0 + timedelta(days=1) - timedelta(hours=24) for w in src.scans)
    assert state.get_mailbox_state("a@x.com").last_wide_scan is not None


# ---------- 实例锁 ----------

def test_lock_is_exclusive(tmp_path):
    held = acquire_lock(tmp_path / "lock")
    with pytest.raises(LockHeld):
        acquire_lock(tmp_path / "lock")
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    acquire_lock(tmp_path / "lock").close()
