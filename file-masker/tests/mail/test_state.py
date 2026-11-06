import os
import sqlite3
import threading

import pytest

from file_masker.mail.state import ProcessedRow, StateDb


@pytest.fixture
def db(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return StateDb(d / "state.db")


def row(address="a@x.com", mail_id="m1", tenant="lq", status="written", **kw):
    base = dict(address=address, mail_id=mail_id, tenant=tenant, status=status, reason="", subject="S",
                hits=1, attachments=1, location="/srv/masked/lq/x", at="2026-10-16T10:00:00+08:00")
    base.update(kw)
    return ProcessedRow(**base)


def test_creates_tables_and_private_files(db):
    names = {r[0] for r in db.connection().execute("select name from sqlite_master where type='table'")}
    assert {"mailbox_state", "processed", "rules", "oauth_token", "audit"} <= names
    assert db.connection().execute("pragma journal_mode").fetchone()[0] == "wal"
    for suffix in ("", "-wal", "-shm"):
        p = str(db.path) + suffix
        if os.path.exists(p):
            assert oct(os.stat(p).st_mode)[-3:] == "600", p


def test_rules_per_tenant(db):
    assert db.get_rules("lq") == ()
    db.replace_rules("lq", [("Zhang San", "CLIENT-A"), ("张三", "")])
    db.replace_rules("look", [("Li Si", "X")])
    assert db.get_rules("lq") == (("Zhang San", "CLIENT-A"), ("张三", ""))
    assert db.get_rules("look") == (("Li Si", "X"),)
    db.replace_rules("lq", [])
    assert db.get_rules("lq") == () and db.get_rules("look") == (("Li Si", "X"),)


def test_mailbox_state_lifecycle(db):
    assert db.get_mailbox_state("a@x.com") is None
    db.init_mailbox_state("a@x.com", cursor={"uidvalidity": 7, "last_uid": 41}, not_before="2026-10-16T09:00:00+08:00")
    st = db.get_mailbox_state("a@x.com")
    assert st.cursor == {"uidvalidity": 7, "last_uid": 41} and st.not_before == "2026-10-16T09:00:00+08:00"
    assert st.scan_until is None and st.last_wide_scan is None and st.first_run_at
    db.set_scan_until("a@x.com", "2026-10-02T00:00:00+08:00")
    db.set_last_wide_scan("a@x.com", "2026-10-16T00:10:00+08:00")
    st = db.get_mailbox_state("a@x.com")
    assert st.scan_until == "2026-10-02T00:00:00+08:00" and st.last_wide_scan == "2026-10-16T00:10:00+08:00"
    db.set_scan_until("a@x.com", None)
    assert db.get_mailbox_state("a@x.com").scan_until is None
    db.init_mailbox_state("a@x.com", cursor={"x": 1}, not_before="later")  # 已存在则不覆盖
    assert db.get_mailbox_state("a@x.com").cursor == {"uidvalidity": 7, "last_uid": 41}


def test_record_processed_and_advance_is_one_transaction(db):
    db.init_mailbox_state("a@x.com", cursor={"last_uid": 1}, not_before="t0")
    db.record_processed_and_advance(row(mail_id="m1"), cursor={"last_uid": 5})
    assert db.is_processed("a@x.com", "m1") and not db.is_processed("a@x.com", "m2")
    assert db.get_mailbox_state("a@x.com").cursor == {"last_uid": 5}
    # 游标目标邮箱不存在 → 整个事务回滚，processed 也不能留下
    with pytest.raises(LookupError):
        db.record_processed_and_advance(row(address="ghost@x.com", mail_id="m9"), cursor={"last_uid": 9})
    assert not db.is_processed("ghost@x.com", "m9")
    # 重复记录同一封 → 覆盖而不是报错
    db.record_processed_and_advance(row(mail_id="m1", status="held", reason="x"), cursor={"last_uid": 5})
    assert db.recent("lq")[0].status == "held"


def test_processed_skipped_and_original_round_trip(db):
    db.init_mailbox_state("a@x.com", {}, "2026-10-16T00:00:00+00:00")
    db.record_processed_and_advance(row(skipped=2, original="/srv/originals/lq/a/x.eml"), {})
    rec = db.recent("lq")[0]
    assert (rec.skipped, rec.original) == (2, "/srv/originals/lq/a/x.eml")


def test_old_processed_table_gains_new_columns(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:  # 1.3.0 的表结构
        conn.execute("CREATE TABLE processed (address TEXT NOT NULL, mail_id TEXT NOT NULL, tenant TEXT NOT NULL,"
                     " status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',"
                     " hits INTEGER NOT NULL DEFAULT 0, attachments INTEGER NOT NULL DEFAULT 0,"
                     " location TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, PRIMARY KEY (address, mail_id))")
        conn.execute("INSERT INTO processed VALUES ('a@x.com','old','lq','held','no_rules','',0,0,'','2026-10-16T10:00:00+08:00')")
    db = StateDb(path)
    old = db.recent("lq")[0]
    assert (old.mail_id, old.skipped, old.original) == ("old", 0, "")
    StateDb(path)  # 再次打开不重复加列


def test_advance_cursor_only(db):
    db.init_mailbox_state("a@x.com", cursor={"last_uid": 1}, not_before="t0")
    db.advance_cursor("a@x.com", {"last_uid": 3})
    assert db.get_mailbox_state("a@x.com").cursor == {"last_uid": 3}


def test_recent_is_per_tenant_newest_first_and_capped(db):
    db.init_mailbox_state("a@x.com", cursor={}, not_before="t0")
    db.init_mailbox_state("b@x.com", cursor={}, not_before="t0")
    for i in range(60):
        db.record_processed_and_advance(row(mail_id=f"m{i:03d}", at=f"2026-10-16T10:{i:02d}:00+08:00"), cursor={})
    db.record_processed_and_advance(row(address="b@x.com", tenant="look", mail_id="other"), cursor={})
    recent = db.recent("lq")
    assert len(recent) == 50 and recent[0].mail_id == "m059" and recent[-1].mail_id == "m010"
    assert [r.mail_id for r in db.recent("look")] == ["other"]
    assert db.recent("lq", limit=5)[0].at == "2026-10-16T10:59:00+08:00"


def test_purge_processed_before(db):
    db.init_mailbox_state("a@x.com", cursor={}, not_before="t0")
    db.record_processed_and_advance(row(mail_id="old", at="2026-01-01T00:00:00+08:00"), cursor={})
    db.record_processed_and_advance(row(mail_id="new", at="2026-10-16T00:00:00+08:00"), cursor={})
    assert db.purge_processed_before("2026-07-01T00:00:00+08:00") == 1
    assert not db.is_processed("a@x.com", "old") and db.is_processed("a@x.com", "new")


def test_oauth_token_lifecycle(db):
    assert db.get_token("a@x.com") is None
    db.save_token("a@x.com", provider="google", account="A@X.com", refresh_token="r1")
    t = db.get_token("a@x.com")
    assert (t.provider, t.account, t.refresh_token, t.status, t.auth_generation) == ("google", "A@X.com", "r1", "ok", 0)
    db.rotate_refresh_token("a@x.com", "r2")
    db.rotate_refresh_token("a@x.com", None)  # 响应不含新 token → 保留
    assert db.get_token("a@x.com").refresh_token == "r2"
    db.mark_expired("a@x.com")
    assert db.get_token("a@x.com").status == "expired"
    db.save_token("a@x.com", provider="google", account="a@x.com", refresh_token="r3")  # 重新授权 → 回到 ok
    assert db.get_token("a@x.com").status == "ok"
    assert db.bump_generation("a@x.com") == 1 and db.bump_generation("a@x.com") == 2
    assert db.get_generation("a@x.com") == 2
    db.delete_token("a@x.com")
    assert db.get_token("a@x.com") is None
    assert db.get_generation("a@x.com") == 2  # 断开后 generation 仍保留，在途流程据此判断失效
    assert db.bump_generation("never@x.com") == 1


def test_audit(db):
    db.audit("rules.replace", "lq", client="10.0.0.5")
    db.audit("oauth.disconnect", "a@x.com")
    entries = db.recent_audit()
    assert [(e.action, e.target, e.client) for e in entries] == [("oauth.disconnect", "a@x.com", ""), ("rules.replace", "lq", "10.0.0.5")]
    assert all(e.at for e in entries)


def test_concurrent_writers_from_threads(db):
    db.init_mailbox_state("a@x.com", cursor={}, not_before="t0")
    errors = []

    def writer(n):
        try:
            for i in range(100):
                db.record_processed_and_advance(row(mail_id=f"t{n}-{i}"), cursor={"n": n})
                db.replace_rules("lq", [(f"kw{n}{i:02d}x", "")])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert db.connection().execute("select count(*) from processed").fetchone()[0] == 400
    assert db.connection().execute("pragma integrity_check").fetchone()[0] == "ok"


def test_rejects_unreadable_path(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        StateDb(tmp_path / "missing-dir" / "state.db")
