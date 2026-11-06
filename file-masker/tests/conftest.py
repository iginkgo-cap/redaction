import fitz
import pytest


def make_pdf(lines, fontname="helv", fontsize=11, rotate=0, color=(0, 0, 0)):
    """lines: [(text, y)] 或 [str]；返回 PDF 字节。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for i, item in enumerate(lines):
        text, y = item if isinstance(item, tuple) else (item, 80 + 30 * i)
        page.insert_text((50, y), text, fontname=fontname, fontsize=fontsize, color=color)
    if rotate:
        page.set_rotation(rotate)
    return doc.tobytes()


def page_text(pdf_bytes, n=0):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc[n].get_text()


def spans(pdf_bytes, n=0):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return [s for b in doc[n].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]


@pytest.fixture
def pdf():
    return make_pdf
