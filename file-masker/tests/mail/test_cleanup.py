from datetime import datetime, timedelta, timezone

import pytest

from file_masker.mail.state import ProcessedRow, StateDb
from file_masker.mail.worker import cleanup

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


def mkdir(root, *parts, files=("mail.json", ".ready")):
    d = root.joinpath(*parts)
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_text("x")
    return d


def test_cleanup_removes_expired_day_dirs_tmp_dirs_and_processed_rows(tmp_path):
    root = tmp_path / "masked"
    old = mkdir(root, "lq", "a@x.com", "2026-07-01", "abc")
    edge = mkdir(root, "lq", "a@x.com", "2026-07-18", "abc")   # 正好 90 天：保留
    keep = mkdir(root, "lq", "a@x.com", "2026-10-16", "abc")
    tmp = mkdir(root, "look", "b@x.com", "2026-10-16", "abc.tmp-1a2b", files=("mail.json",))
    weird = mkdir(root, "lq", "a@x.com", "notadate", "abc")
    stray = root / "lq" / "stray.txt"
    stray.write_text("x")
    state = StateDb(tmp_path / "state.db")
    state.init_mailbox_state("a@x.com", {}, "2026-01-01T00:00:00+00:00")
    local = lambda d: datetime(*d, tzinfo=timezone.utc).astimezone().isoformat(timespec="seconds")  # 与 worker._now_text 同一时区
    for mail_id, at in (("old", local((2026, 7, 1))), ("new", local((2026, 10, 16, 9)))):
        state.record_processed_and_advance(ProcessedRow("a@x.com", mail_id, "lq", "held", "", "", 0, 0, "", at), {})
    removed = cleanup(root, 90, state, NOW, "Asia/Shanghai")
    assert removed == {"day_dirs": 1, "tmp_dirs": 1, "processed_rows": 1}
    assert not old.exists() and edge.exists() and keep.exists() and not tmp.exists()
    assert weird.exists() and stray.exists()   # 不认识的目录名、散文件不碰
    assert not state.is_processed("a@x.com", "old") and state.is_processed("a@x.com", "new")


def test_cleanup_also_expires_originals(tmp_path):
    root, originals = tmp_path / "masked", tmp_path / "originals"
    root.mkdir()
    old = mkdir(originals, "lq", "a@x.com", "2026-07-01", files=())
    (old / "abc.eml").write_text("x")
    keep = mkdir(originals, "lq", "a@x.com", "2026-10-16", files=())
    stale_tmp = keep / "abc.eml.tmp-1a2b"
    stale_tmp.write_text("x")
    removed = cleanup(root, 90, StateDb(tmp_path / "state.db"), NOW, "Asia/Shanghai", originals_root=originals)
    assert removed["day_dirs"] == 1 and removed["tmp_dirs"] == 1
    assert not old.exists() and keep.exists() and not stale_tmp.exists()


@pytest.mark.parametrize("root", ["/", "/srv", "/Users", "relative/path"])
def test_cleanup_refuses_unsafe_roots(tmp_path, root):
    state = StateDb(tmp_path / "state.db")
    with pytest.raises(ValueError):
        cleanup(root, 90, state, NOW, "UTC")


def test_cleanup_tolerates_missing_root(tmp_path):
    state = StateDb(tmp_path / "state.db")
    assert cleanup(tmp_path / "missing" / "masked", 90, state, NOW, "UTC") == {"day_dirs": 0, "tmp_dirs": 0, "processed_rows": 0}
