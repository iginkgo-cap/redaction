import hashlib
import json
import os
import unicodedata
from datetime import datetime, timezone

import pytest

from conftest import make_pdf
from file_masker.mail.mime import MaskedMail
from file_masker.mail.sink import Delivered, DirConflict, FsSink, MailContext, dir_id, safe_filename

PDF = make_pdf(["hello"])
RECEIVED = datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc)  # 北京时间 10-16 07:30


def masked(**kw):
    base = dict(subject="CLIENT-A 9月结单", original_from="RM <rm@bank.example>", original_date="Thu, 15 Oct 2026 23:00:00 +0000",
                text_body="plain", html_body="<p>html</p>", attachments=[("CLIENT-A statement.pdf", PDF)],
                removed_images=1, hits=7, message_id="<abc@mail.example.com>",
                original_datetime=datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc))
    base.update(kw)
    return MaskedMail(**base)


def ctx(**kw):
    base = dict(tenant="lq", source="lq-ops@example.com", provider="gmail", mail_id="18f3a2c9", received_at=RECEIVED,
                rules_version="sha256:abc")
    base.update(kw)
    return MailContext(**base)


@pytest.fixture
def sink(tmp_path):
    return FsSink(tmp_path / "masked", "Asia/Shanghai", "1.3.0")


def test_dir_id_formula():
    assert dir_id("gmail", "18f3a2c9") == hashlib.sha256(b"gmail\x0018f3a2c9").hexdigest()[:32]
    assert dir_id("gmail", "x") != dir_id("graph", "x")


def test_deliver_writes_layout_and_mail_json(sink, tmp_path):
    out = sink.deliver(masked(), ctx())
    d = tmp_path / "masked" / "lq" / "lq-ops@example.com" / "2026-10-16" / dir_id("gmail", "18f3a2c9")
    assert isinstance(out, Delivered) and out.location == str(d) and not out.reused
    assert (out.subject, out.hits, out.attachments) == ("CLIENT-A 9月结单", 7, 1)
    assert sorted(p.name for p in d.iterdir()) == [".ready", "CLIENT-A statement.pdf", "mail.json"]
    assert (d / "CLIENT-A statement.pdf").read_bytes() == PDF
    meta = json.loads((d / "mail.json").read_text())
    assert meta["tenant"] == "lq" and meta["source"] == "lq-ops@example.com" and meta["provider"] == "gmail"
    assert meta["mail_id"] == "18f3a2c9" and meta["message_id"] == "<abc@mail.example.com>"
    assert meta["dedup_fingerprint"] == "sha256:" + hashlib.sha256(b"<abc@mail.example.com>").hexdigest()
    assert meta["original_date"] == "2026-10-15T23:00:00+00:00" and meta["received_at"] == "2026-10-15T23:30:00+00:00"
    assert meta["from_alias"] == "RM <rm@bank.example>" and meta["subject"] == "CLIENT-A 9月结单"
    assert meta["body_text"] == "plain" and meta["body_html"] == "<p>html</p>"
    assert meta["attachments"] == ["CLIENT-A statement.pdf"] and meta["removed_images"] == 1 and meta["hits"] == 7
    assert meta["skipped"] == []
    assert meta["rules_version"] == "sha256:abc" and meta["masker_version"] == "1.3.0" and meta["masked_at"]
    assert not list(d.parent.glob("*.tmp-*"))
    # 权限：目录 750、文件 640（不依赖进程 umask）
    assert oct(d.stat().st_mode)[-3:] == "750"
    for parent in (d.parent, d.parent.parent, d.parent.parent.parent):  # 日期 / 邮箱 / 租户层也不跟 umask 走
        assert oct(parent.stat().st_mode)[-3:] == "750"
    assert oct((d / "mail.json").stat().st_mode)[-3:] == "640" and oct((d / ".ready").stat().st_mode)[-3:] == "640"


def test_fingerprint_without_message_id_and_null_dates(sink, tmp_path):
    out = sink.deliver(masked(message_id=None, original_datetime=None), ctx())
    meta = json.loads((tmp_path / out.location / "mail.json").read_text())
    expected = hashlib.sha256("2026-10-15T23:30:00+00:00|CLIENT-A 9月结单|1|{}".format(len(PDF)).encode()).hexdigest()
    assert meta["message_id"] is None and meta["original_date"] is None
    assert meta["dedup_fingerprint"] == "sha256:" + expected


def test_existing_ready_with_same_mail_id_is_reused(sink, tmp_path):
    first = sink.deliver(masked(), ctx())
    before = (tmp_path / first.location / "mail.json").stat().st_mtime_ns
    again = sink.deliver(masked(subject="DIFFERENT after rule change", hits=99), ctx())
    assert again.reused and again.location == first.location
    assert (again.subject, again.hits, again.attachments) == ("CLIENT-A 9月结单", 7, 1)  # 以已有 mail.json 为准（E7）
    assert (tmp_path / first.location / "mail.json").stat().st_mtime_ns == before


def test_existing_ready_with_other_mail_id_or_corrupt_is_conflict(sink, tmp_path):
    first = sink.deliver(masked(), ctx())
    d = tmp_path / first.location
    meta = json.loads((d / "mail.json").read_text())
    meta["mail_id"] = "someone-else"
    (d / "mail.json").write_text(json.dumps(meta))
    with pytest.raises(DirConflict):
        sink.deliver(masked(), ctx())
    (d / "mail.json").write_text("{not json")
    with pytest.raises(DirConflict):
        sink.deliver(masked(), ctx())


def test_partial_dir_without_ready_is_rewritten(sink, tmp_path):
    d = tmp_path / "masked" / "lq" / "lq-ops@example.com" / "2026-10-16" / dir_id("gmail", "18f3a2c9")
    d.mkdir(parents=True)
    (d / "garbage.bin").write_bytes(b"half written")
    out = sink.deliver(masked(), ctx())
    assert not out.reused and not (d / "garbage.bin").exists() and (d / ".ready").exists()


@pytest.mark.parametrize("name,expected", [
    ("../escape.pdf", "escape.pdf"),
    ("a/b.pdf", "a_b.pdf"),
    ("dir\\c.pdf", "dir_c.pdf"),
    ("bad\x00\x1fname.pdf", "badname.pdf"),
    ("..", "attachment.pdf"),
    (".pdf", "attachment.pdf"),
    ("noext", "noext.pdf"),
    ("  spaced .PDF ", "spaced.PDF"),
])
def test_safe_filename(name, expected):
    assert safe_filename(name, set()) == expected


def test_safe_filename_truncates_by_utf8_bytes():
    long = "结" * 80 + ".pdf"  # 240 字节的主干
    out = safe_filename(long, set())
    assert out.endswith(".pdf") and len(out[:-4].encode()) <= 100 and out[:-4] == "结" * 33


def test_safe_filename_dedupes_case_and_nfc():
    used = set()
    assert safe_filename("Report.pdf", used) == "Report.pdf"
    assert safe_filename("report.PDF", used) == "report (1).PDF"
    assert safe_filename("ab.pdf", used) == "ab.pdf" and safe_filename("a/b.pdf", used) == "a_b.pdf"
    nfd = unicodedata.normalize("NFD", "é.pdf")
    assert safe_filename("é.pdf", used) == "é.pdf" and safe_filename(nfd, used) == "é (1).pdf"


def test_attachments_cannot_escape_and_are_recorded_by_final_name(sink, tmp_path):
    out = sink.deliver(masked(attachments=[("../../escape.pdf", PDF), ("A.pdf", PDF), ("a.pdf", PDF)]), ctx())
    d = tmp_path / out.location
    meta = json.loads((d / "mail.json").read_text())
    assert meta["attachments"] == ["escape.pdf", "A.pdf", "a (1).pdf"]
    assert sorted(p.name for p in d.iterdir() if p.suffix == ".pdf") == ["A.pdf", "a (1).pdf", "escape.pdf"]
    assert not (tmp_path / "escape.pdf").exists() and not (tmp_path / "masked" / "escape.pdf").exists()


def test_date_dir_uses_received_at_in_configured_timezone(tmp_path):
    utc_sink = FsSink(tmp_path / "m", "UTC", "1.3.0")
    out = utc_sink.deliver(masked(), ctx())
    assert "/2026-10-15/" in out.location  # 同一时刻在 UTC 还是 10-15
