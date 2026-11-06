"""v2 file 项（LOOK-6876，ADR-002 §3.2）：/v2/redact 处理 PDF；/v2/detect 不收文件项。

复用 test_v2_api 的 fixture：nodb 无库；db 连 REDACTION_TEST_DATABASE_URL，装了把「张三」「老王」识别成 PERSON 的假模型。
"""
from __future__ import annotations

import base64

import fitz

from app import main
from app.v2 import api

from .test_v2_api import _with_model, db, nodb  # noqa: F401  fixture


def make_pdf(pages: list[list[tuple[str, str]] | None]) -> bytes:
    """每页一组（文字, 字体）行；None = 整页图片的扫描页。中文用 china-s，其余用 helv。"""
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page(width=595, height=842)
        if lines is None:
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 80), 0)
            pix.set_rect(pix.irect, (200, 120, 40))
            page.insert_image(page.rect, pixmap=pix)
            continue
        for i, (text, font) in enumerate(lines):
            page.insert_text((50, 80 + 30 * i), text, fontname=font, fontsize=11)
    return doc.tobytes()


def file_item(data: bytes, id_: str = "f1", content_type: str = "application/pdf") -> dict:
    return {"id": id_, "file": {"name": "a.pdf", "content_type": content_type,
                                "data": base64.b64encode(data).decode()}}


def text_of(b64: str, n: int = 0) -> str:
    with fitz.open(stream=base64.b64decode(b64), filetype="pdf") as doc:
        return doc[n].get_text()


# 用三字名：两字中文名约 22pt 宽，放不下 [[PERSON_n]]（< 4pt）会退成纯色块（E16），见下方单测
STATEMENT = [[("客户 张三丰", "china-s"), ("Phone 13800138000", "helv")]]


def test_file_项_人名电话换成占位符_与同请求_text_项一致(db) -> None:
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [
        {"id": "m1", "text": "张三丰的电话"}, file_item(make_pdf(STATEMENT))]})

    assert r.status_code == 200, r.text
    body = r.json()
    m1, f1 = body["items"]
    assert f1["file"]["content_type"] == "application/pdf"
    assert f1["pages"] == [{"page": 1, "scanned": False}]
    person = next(e for e in body["entities"] if e["value"] == "张三丰")
    phone = next(e for e in body["entities"] if e["kind"] == "PHONE")
    out = text_of(f1["file"]["data"])
    assert "张三丰" not in out and "13800138000" not in out
    assert person["placeholder"] in out and phone["placeholder"] in out
    assert m1["text"].startswith(person["placeholder"])
    file_occ = [o for o in person["occurrences"] if o["item"] == "f1"]
    assert len(file_occ) == 1 and file_occ[0]["page"] == 1
    x0, y0, x1, y1 = file_occ[0]["bbox"]
    assert 0 <= x0 < x1 <= 595 and 0 <= y0 < y1 <= 842
    assert "start" not in file_occ[0]


def test_file_项_两字中文名放不下占位符时涂成色块_映射照常(db) -> None:
    data = make_pdf([[("客户 张三", "china-s")]])
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [file_item(data)]})

    body = r.json()
    out = text_of(body["items"][0]["file"]["data"])
    assert "张三" not in out and "PERSON" not in out
    assert [(e["value"], e["kind"]) for e in body["entities"]] == [("张三", "PERSON")]
    assert body["entities"][0]["occurrences"][0]["page"] == 1


def test_file_项_扫描页标记且不识别(db) -> None:
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [file_item(make_pdf([STATEMENT[0], None]))]})

    assert r.json()["items"][0]["pages"] == [{"page": 1, "scanned": False}, {"page": 2, "scanned": True}]


def test_file_项_无实体时原样返回(db) -> None:
    data = make_pdf([[("Nothing to hide here", "helv")]])
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [file_item(data)]})

    body = r.json()
    assert body["entities"] == []
    assert base64.b64decode(body["items"][0]["file"]["data"]) == data


def test_file_项_已有占位符原样保留(db) -> None:
    data = make_pdf([[("Ref [[PERSON_9]] ok", "helv"), ("老王", "china-s")]])
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [file_item(data)]})

    body = r.json()
    out = text_of(body["items"][0]["file"]["data"])
    assert "[[PERSON_9]]" in out and "老王" not in out
    assert [e["value"] for e in body["entities"]] == ["老王"]


def test_file_项_校验失败_400(db) -> None:
    good = file_item(make_pdf(STATEMENT))
    bad_items = [
        file_item(b"plain text", content_type="text/plain"),
        {"id": "f1", "file": {**good["file"], "data": "@@not base64@@"}},
        {**good, "text": "两个载体"},
        {"id": "f1", "file": None},
    ]
    for item in bad_items:
        r = db.post("/v2/redact", json={"namespace": "n1", "items": [item]})
        assert r.status_code == 400 and r.json()["code"] == "invalid_request", item


def test_file_项_损坏_422(db) -> None:
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [file_item(b"%PDF-1.7 broken")]})
    assert r.status_code == 422 and r.json()["code"] == "file_unreadable"


def test_file_项_超字节_413(db, monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "max_file_bytes", 100)
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [file_item(make_pdf(STATEMENT))]})
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"


def test_file_项_抽出的字数计入总字数上限(db, monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "max_request_chars", 5)
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [file_item(make_pdf(STATEMENT))]})
    assert r.status_code == 413 and r.json()["code"] == "text_too_large"


def test_含_file_项时每个请求同时只识别一段(db, monkeypatch) -> None:
    seen = []
    original = api.detect_texts

    async def spy(texts, **kw):
        seen.append(kw.get("segments_in_flight"))
        return await original(texts, **kw)

    monkeypatch.setattr(api, "detect_texts", spy)
    db.post("/v2/redact", json={"namespace": "n1", "items": [{"id": "m1", "text": "张三"}]})
    db.post("/v2/redact", json={"namespace": "n1", "items": [file_item(make_pdf(STATEMENT))]})

    assert seen[1] == 1 and seen[0] != 1


def test_detect_不收_file_项(nodb) -> None:
    for item in (file_item(make_pdf(STATEMENT)), {"id": "f1", "file": None}):
        r = nodb.post("/v2/detect", json={"namespace": "x", "items": [item]})
        assert r.status_code == 400 and r.json()["code"] == "invalid_request", item


def test_无库_redact_file_项_invalid_request(nodb) -> None:
    _with_model()
    r = nodb.post("/v2/redact", json={"namespace": "x", "items": [file_item(make_pdf(STATEMENT))]})
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"
