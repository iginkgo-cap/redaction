"""page_text / offsets_to_parts / replace_spans：服务按整页文本识别、按码点区间写回（LOOK-6876）。"""
import fitz
import pytest
from conftest import make_pdf, spans

from file_masker import engine


def open_page(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return doc, doc[0]


def locate(pt, value):
    start = pt.text.index(value)
    return start, start + len(value)


def redact(pdf_bytes, values_to_text):
    """在整页文本里找每个值，换成对应文字，返回（新 PDF, replace_spans 的返回值）。"""
    doc, page = open_page(pdf_bytes)
    pt = engine.page_text(page)
    reps = [engine.Replacement(engine.offsets_to_parts(pt, *locate(pt, v)), t) for v, t in values_to_text.items()]
    rects = engine.replace_spans(page, reps)
    out = doc.tobytes(garbage=4, clean=True, deflate=True)
    doc.close()
    return out, rects


def text_of(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc[0].get_text()


def test_page_text_joins_lines_and_maps_every_char():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 80), "账户持有人 张三", fontname="china-s", fontsize=11)
    page.insert_text((50, 110), "Client Zhang San", fontname="helv", fontsize=11)
    pt = engine.page_text(page)
    doc.close()

    assert pt.text.split("\n") == ["账户持有人 张三", "Client Zhang San"]
    for i, ch in enumerate(pt.text):
        if ch == "\n":
            assert pt.pos[i] is None
        else:
            line, k = pt.pos[i]
            assert pt.lines[line][k].c == ch


def test_offsets_skip_whitespace_inside_a_line():
    doc, page = open_page(make_pdf(["Client Zhang San Account"]))
    pt = engine.page_text(page)
    parts = engine.offsets_to_parts(pt, *locate(pt, "Zhang San"))
    doc.close()

    assert len(parts) == 1
    chars, idx = parts[0]
    assert "".join(chars[i].c for i in idx) == "ZhangSan"


def test_offsets_split_by_line():
    doc, page = open_page(make_pdf(["Account holder Zhang", "San Limited"]))
    pt = engine.page_text(page)
    parts = engine.offsets_to_parts(pt, *locate(pt, "Zhang\nSan"))
    doc.close()

    assert ["".join(chars[i].c for i in idx) for chars, idx in parts] == ["Zhang", "San"]


def test_placeholder_replaces_value():
    out, rects = redact(make_pdf(["Client Zhang San Account"]), {"Zhang San": "[[PERSON_1]]"})

    text = text_of(out)
    assert "Zhang" not in text and "San " not in text
    assert "[[PERSON_1]]" in text and "Client" in text and "Account" in text
    assert len(rects) == 1 and len(rects[0]) == 1


def test_longer_placeholder_shrinks_font():
    out, _ = redact(make_pdf(["Client Zhang San Account"], fontsize=11), {"Zhang San": "[[PERSON_1]]"})

    placeholder = next(s for s in spans(out) if "[[PERSON_1]]" in s["text"])
    assert engine.MIN_FONT_SIZE <= placeholder["size"] < 11


def test_cross_line_value_writes_text_on_first_line_only():
    out, rects = redact(make_pdf(["Account holder Zhang", "San Limited"]), {"Zhang\nSan": "[[PERSON_1]]"})

    text = text_of(out)
    assert text.count("[[PERSON_1]]") == 1
    assert "Zhang" not in text and "San" not in text and "Limited" in text
    # 文字画在第一行的矩形里（抽字顺序里它排在最后，按位置判断）
    first_line, second_line = rects[0]
    placeholder = next(s for s in spans(out) if "[[PERSON_1]]" in s["text"])
    assert fitz.Rect(placeholder["bbox"]).intersects(first_line)
    assert not fitz.Rect(placeholder["bbox"]).intersects(second_line)


def test_too_small_falls_back_to_solid_fill():
    out, _ = redact(make_pdf(["Al Bank"]), {"Al": "[[ORGANIZATION_123]]"})

    text = text_of(out)
    assert "Al" not in text and "ORGANIZATION" not in text and "Bank" in text


def test_rects_lie_inside_page():
    pdf = make_pdf(["Client Zhang San Account", "Phone 13800138000"])
    out, rects = redact(pdf, {"Zhang San": "[[PERSON_1]]", "13800138000": "[[PHONE_1]]"})

    page_rect = fitz.Rect(0, 0, 595, 842)
    assert all(r in page_rect and not r.is_empty for group in rects for r in group)
    assert "13800138000" not in text_of(out)


def test_no_spans_leaves_page_untouched():
    pdf = make_pdf(["Client Zhang San Account"])
    doc, page = open_page(pdf)
    assert engine.replace_spans(page, []) == []
    assert page.get_text() == text_of(pdf)
    doc.close()


@pytest.mark.parametrize("value", ["Zhang San", "Client"])
def test_keyword_mode_still_goes_through_replace_spans(monkeypatch, value):
    calls = []
    original = engine.replace_spans
    monkeypatch.setattr(engine, "replace_spans", lambda page, reps: calls.append(len(reps)) or original(page, reps))

    engine.mask_pdf(make_pdf(["Client Zhang San Account"]), {value: "X"})

    assert calls == [1]


def test_ligature_is_expanded_so_the_whole_value_is_replaced():
    # 「ﬁ」连字若保留为一个字符，识别出的邮箱会从连字之后开始，前缀明文残留（helv 无此字形，用 cjk 字体）
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    writer = fitz.TextWriter(page.rect)
    writer.append((50, 80), "Contact soﬁa@example.com today", font=fitz.Font("cjk"), fontsize=11)
    writer.write_text(page)
    pdf = doc.tobytes()
    doc.close()
    doc, page = open_page(pdf)
    pt = engine.page_text(page)
    doc.close()
    assert "sofia@example.com" in pt.text

    out, _ = redact(pdf, {"sofia@example.com": "[[EMAIL_1]]"})
    text = text_of(out)
    assert "[[EMAIL_1]]" in text
    assert "soﬁ" not in text and "sofi" not in text and "example.com" not in text
