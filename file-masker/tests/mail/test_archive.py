import io
import zipfile

import pytest

from conftest import make_pdf
from file_masker.mail import archive
from file_masker.mail.archive import ArchiveError, expand_zip

PDF = make_pdf(["hello"])


def make_zip(entries, encrypt=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    data = buf.getvalue()
    if encrypt:  # writestr 会重置 flag_bits，只能写完后直接改本地头与中央目录里的加密位
        data = bytearray(data)
        for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            pos = data.find(sig)
            data[pos + offset] |= 0x1
        data = bytes(data)
    return data


def test_expands_pdfs():
    out = expand_zip(make_zip([("a.pdf", PDF), ("dir/b.PDF", PDF)]))
    assert [name for name, _ in out] == ["a.pdf", "b.PDF"]
    assert all(data == PDF for _, data in out)


def test_ignores_mac_metadata_and_dirs():
    out = expand_zip(make_zip([("dir/", b""), ("__MACOSX/._a.pdf", b"x"), (".DS_Store", b"x"), ("a.pdf", PDF)]))
    assert [name for name, _ in out] == ["a.pdf"]


def test_gbk_entry_name_without_utf8_flag():
    gbk = "结单.pdf".encode("gbk")
    placeholder = b"X" * (len(gbk) - 4) + b".pdf"
    data = make_zip([(placeholder.decode(), PDF)]).replace(placeholder, gbk)
    assert expand_zip(data)[0][0] == "结单.pdf"


def test_utf8_entry_name():
    assert expand_zip(make_zip([("结单.pdf", PDF)]))[0][0] == "结单.pdf"


@pytest.mark.parametrize("data", [
    make_zip([("a.pdf", PDF)], encrypt=True),
    make_zip([("inner.zip", make_zip([("a.pdf", PDF)]))]),
    make_zip([("a.pdf", PDF), ("b.xlsx", b"PK...")]),
    make_zip([("fake.pdf", b"not a pdf")]),
    make_zip([]),
    b"not a zip",
])
def test_rejected(data):
    with pytest.raises(ArchiveError):
        expand_zip(data)


def test_too_many_entries():
    with pytest.raises(ArchiveError, match="entries"):
        expand_zip(make_zip([(f"{i}.pdf", PDF) for i in range(archive.MAX_ENTRIES + 1)]))


def test_too_large_after_inflate(monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", len(PDF) + 10)
    with pytest.raises(ArchiveError, match="large"):
        expand_zip(make_zip([("a.pdf", PDF), ("b.pdf", PDF)]))
