import json

import pytest
from fastapi.testclient import TestClient

from conftest import make_pdf
from file_masker.web import app as web

client = TestClient(web.app)
KW = json.dumps([{"source": "Zhang San", "target": "***"}])


def post(data=None, keywords=KW, name="a.pdf"):
    data = make_pdf(["Client Zhang San"]) if data is None else data
    return client.post("/mask", files={"file": (name, data, "application/pdf")}, data={"keywords": keywords})


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_index_serves_built_frontend(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<title>PDF Masking</title>")
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path)
    resp = client.get("/")
    assert "PDF Masking" in resp.text
    assert resp.headers["cache-control"] == "no-cache"


def test_index_without_build_returns_503(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path)
    assert client.get("/").status_code == 503


def test_mask_ok():
    r = post(name="statement.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "statement_masked.pdf" in r.headers["content-disposition"]


@pytest.mark.parametrize("kw,msg", [("not json", "Invalid"), ("[]", "at least one"), (json.dumps([{"source": "ab", "target": ""}]), "3 characters")])
def test_bad_keywords(kw, msg):
    r = post(keywords=kw)
    assert r.status_code == 400 and msg in r.json()["error"]


@pytest.mark.parametrize("source", ["张三", "欧阳娜娜", "Zhang"])
def test_short_keywords_allowed(source):
    assert post(keywords=json.dumps([{"source": source, "target": "X"}])).status_code == 200


@pytest.mark.parametrize("source", ["Li", "12", "张", "张a"])
def test_short_keywords_rejected(source):
    r = post(keywords=json.dumps([{"source": source, "target": "X"}]))
    assert r.status_code == 400 and "2 for Chinese" in r.json()["error"]


def test_a_b_keyword_is_allowed():
    assert post(keywords=json.dumps([{"source": "A B", "target": "X"}])).status_code == 200


def test_not_pdf():
    assert post(data=b"hello").status_code == 400


def test_too_large(monkeypatch):
    monkeypatch.setattr(web, "MAX_FILE_MB", 0)
    assert post().status_code == 413


def test_scanned_returns_422_without_keyword_leak():
    import fitz
    doc = fitz.open(); p = doc.new_page()
    p.insert_image(p.rect, pixmap=fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), 0))
    r = post(data=doc.tobytes())
    assert r.status_code == 422 and "scanned" in r.json()["error"] and "Zhang" not in r.text


def test_too_many_pages(monkeypatch):
    monkeypatch.setattr(web, "MAX_PAGES", 0)
    assert post().status_code == 422


def test_queue_full_returns_503(monkeypatch):
    import asyncio
    monkeypatch.setattr(web, "_slots", asyncio.Semaphore(0))
    monkeypatch.setattr(web, "QUEUE_TIMEOUT_S", 0.1)
    assert post().status_code == 503


def test_processing_timeout_returns_504(monkeypatch):
    import time
    monkeypatch.setattr(web, "mask_pdf", lambda *a: time.sleep(1))
    monkeypatch.setattr(web, "PROCESS_TIMEOUT_S", 0.1)
    assert post().status_code == 504
