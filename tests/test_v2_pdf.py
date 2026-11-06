"""PDF 载体适配（LOOK-6876）：抽字、写回、上限与错误码、线程与信号量。合成 PDF 在测试里用 PyMuPDF 生成。"""
from __future__ import annotations

import asyncio
import threading
import time

import fitz
import pytest

from app.v2 import pdf
from app.v2.errors import ApiError


def make_pdf(pages: list[str | None]) -> bytes:
    """每个元素一页：字符串 = 文字页（\\n 分行），None = 整页图片的扫描页。"""
    doc = fitz.open()
    for content in pages:
        page = doc.new_page(width=595, height=842)
        if content is None:
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 80), 0)
            pix.set_rect(pix.irect, (200, 120, 40))
            page.insert_image(page.rect, pixmap=pix)
        else:
            for i, line in enumerate(content.split("\n")):
                page.insert_text((50, 80 + 30 * i), line, fontname="helv", fontsize=11)
    return doc.tobytes()


def text_of(data: bytes, n: int = 0) -> str:
    with fitz.open(stream=data, filetype="pdf") as doc:
        return doc[n].get_text()


def render(data: bytes, n: int) -> bytes:
    with fitz.open(stream=data, filetype="pdf") as doc:
        return doc[n].get_pixmap().samples


def edit(text: str, value: str, token: str) -> tuple[int, int, str]:
    start = text.index(value)
    return start, start + len(value), token


def test_extract_gives_page_text_and_marks_scanned_pages():
    pages = pdf.extract_sync(make_pdf(["Client Zhang San\nPhone 13800138000", None]))

    assert [(p.page, p.scanned) for p in pages] == [(1, False), (2, True)]
    assert pages[0].text == "Client Zhang San\nPhone 13800138000"
    assert pages[1].text == ""


def test_write_replaces_values_and_returns_bboxes():
    data = make_pdf(["Client Zhang San\nPhone 13800138000"])
    text = pdf.extract_sync(data)[0].text

    out, boxes = pdf.write_sync(data, {1: [edit(text, "Zhang San", "[[PERSON_1]]"),
                                           edit(text, "13800138000", "[[PHONE_1]]")]})

    new = text_of(out)
    assert "Zhang" not in new and "13800138000" not in new
    assert "[[PERSON_1]]" in new and "[[PHONE_1]]" in new
    assert len(boxes[1]) == 2
    for group in boxes[1]:
        for x0, y0, x1, y1 in group:
            assert 0 <= x0 < x1 <= 595 and 0 <= y0 < y1 <= 842


def test_cross_line_value_gives_one_box_per_line():
    data = make_pdf(["Account holder Zhang\nSan Limited"])
    text = pdf.extract_sync(data)[0].text

    _, boxes = pdf.write_sync(data, {1: [edit(text, "Zhang\nSan", "[[PERSON_1]]")]})

    assert len(boxes[1][0]) == 2


def test_write_keeps_scanned_page_rendering():
    data = make_pdf(["Client Zhang San", None])
    text = pdf.extract_sync(data)[0].text

    out, _ = pdf.write_sync(data, {1: [edit(text, "Zhang San", "[[PERSON_1]]")]})

    # 保存会重新序列化整份文件，字节对比不成立，比渲染结果
    assert render(out, 1) == render(data, 1)


def test_write_without_edits_returns_original_bytes():
    data = make_pdf(["Nothing to hide"])
    assert pdf.write_sync(data, {}) == (data, {})


def test_garbage_is_unreadable():
    with pytest.raises(ApiError) as e:
        pdf.extract_sync(b"not a pdf at all")
    assert e.value.code == "file_unreadable" and e.value.status == 422


def test_encrypted_is_unreadable():
    doc = fitz.open(stream=make_pdf(["secret"]), filetype="pdf")
    data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    with pytest.raises(ApiError) as e:
        pdf.extract_sync(data)
    assert e.value.code == "file_unreadable"


def test_too_many_pages(monkeypatch):
    monkeypatch.setattr(pdf.settings, "max_file_pages", 2)
    with pytest.raises(ApiError) as e:
        pdf.extract_sync(make_pdf(["a", "b", "c"]))
    assert e.value.code == "file_too_large" and e.value.status == 413


def test_too_many_bytes(monkeypatch):
    data = make_pdf(["a"])
    monkeypatch.setattr(pdf.settings, "max_file_bytes", len(data) - 1)
    with pytest.raises(ApiError) as e:
        pdf.extract_sync(data)
    assert e.value.code == "file_too_large"


def test_default_limits():
    assert pdf.settings.max_file_bytes == 24_000_000
    assert pdf.settings.max_file_pages == 300
    assert pdf.settings.pdf_concurrency == 1


async def test_pdf_work_is_serialized_and_off_the_event_loop(monkeypatch):
    running, peak = 0, 0
    lock = threading.Lock()

    def slow(data):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.2)  # 同步阻塞，模拟 PyMuPDF
        with lock:
            running -= 1
        return []

    monkeypatch.setattr(pdf, "extract_sync", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    await asyncio.gather(pdf.extract(b"a"), pdf.extract(b"b"))
    t.cancel()

    assert peak == 1  # pdf_concurrency=1：两份 PDF 排队
    assert ticks > 10  # 事件循环在 PDF 处理期间照常运转
