import json
import os
from datetime import datetime, timezone

from file_masker.mail.sink import MailContext, OriginalStore, dir_id

RECEIVED = datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc)  # 北京时间 10-16 07:30


def ctx(mail_id="18f3a2c9"):
    return MailContext("lq", "lq-ops@example.com", "gmail", mail_id, RECEIVED, "sha256:abc")


def test_save_writes_private_eml_and_reason(tmp_path):
    store = OriginalStore(tmp_path / "originals", "Asia/Shanghai")
    path = store.save(b"raw mail", ctx(), "", [("CLIENT-A.xlsx", "unsupported_attachment")], 2)
    day = tmp_path / "originals" / "lq" / "lq-ops@example.com" / "2026-10-16"
    assert path == str(day / f"{dir_id('gmail', '18f3a2c9')}.eml")
    assert open(path, "rb").read() == b"raw mail"
    meta = json.loads((day / f"{dir_id('gmail', '18f3a2c9')}.json").read_text())
    assert meta == {"tenant": "lq", "source": "lq-ops@example.com", "mail_id": "18f3a2c9",
                    "received_at": RECEIVED.isoformat(), "reason": "",
                    "skipped": [{"name": "CLIENT-A.xlsx", "reason": "unsupported_attachment"}], "removed_images": 2}
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    for d in (day, day.parent, day.parent.parent, tmp_path / "originals"):
        assert oct(d.stat().st_mode)[-3:] == "700"  # 原件只给服务自己，不能被下游 agent 读到
    assert [p.name for p in day.iterdir() if ".tmp-" in p.name] == []


def test_save_again_overwrites_in_place(tmp_path):
    store = OriginalStore(tmp_path / "originals", "Asia/Shanghai")
    first = store.save(b"one", ctx(), "no_rules", [])
    assert store.save(b"two", ctx(), "no_rules", []) == first
    assert open(first, "rb").read() == b"two"
