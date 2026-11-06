import fitz
import pytest

from conftest import make_pdf, page_text, spans
from file_masker.engine import ScannedPageError, InvalidPdfError, mask_pdf, mask_pdf_with_stats, choose_font, fold, min_keyword_length


def raw_streams(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        out = []
        for x in range(1, doc.xref_length()):
            try:
                s = doc.xref_stream(x)
            except Exception:
                s = None
            if s:
                out.append(s)
        return out


def test_does_not_leak_original_bytes():
    out = mask_pdf(make_pdf(["595.28 x 841.89 pt"]), {"595.28 x 841.89 pt": "aa"})
    for s in raw_streams(out):
        assert b"595.28" not in s and "595.28".encode("utf-16-be").hex().encode() not in s.lower()
    assert "aa" in page_text(out)


def test_empty_target_removes_text():
    out = mask_pdf(make_pdf(["Client Zhang San Account"]), {"Zhang San": ""})
    t = page_text(out)
    assert "Zhang" not in t and "Client" in t and "Account" in t


@pytest.mark.parametrize("target", ["已打码", "（）１２３"])
def test_cjk_and_fullwidth_targets_not_corrupted(target):
    out = mask_pdf(make_pdf(["Client Zhang San Account"]), {"Zhang San": target})
    t = page_text(out)
    assert target in t and "?" not in t


def test_preserves_text_color():
    out = mask_pdf(make_pdf(["Client Zhang San Account"], color=(0.5, 0.5, 0.5)), {"Zhang San": "MASKED"})
    s = [s for s in spans(out) if "MASKED" in s["text"]][0]
    assert s["color"] == 0x7F7F7F or abs((s["color"] >> 16) - 127) <= 2


@pytest.mark.parametrize("text,key", [("JOHN SMITH x", "John Smith"), ("john smith x", "John Smith"), ("John Smith x", "john smith")])
def test_case_insensitive(text, key):
    t = page_text(mask_pdf(make_pdf([text]), {key: "MASKED"}))
    assert "MASKED" in t and "smith" not in t.lower()


def test_mixed_case_variants_both_masked():
    t = page_text(mask_pdf(make_pdf(["John Smith and JOHN SMITH"]), {"John Smith": "MASKED"}))
    assert t.count("MASKED") == 2


def test_repeat_masking_keeps_font_size_and_line():  # T1
    pdf = make_pdf(["Client Zhang San Account ACME"])
    for _ in range(5):
        pdf = mask_pdf(pdf, {"Client": "Client"})
    sizes = {round(s["size"], 1) for s in spans(pdf) if "Account" in s["text"]}
    assert sizes == {11.0}
    assert "ACME" in page_text(pdf)


def test_repeat_masking_cjk_line_survives():  # T2
    pdf = make_pdf(["客户：张三 账户 李四 ABC Bank"], fontname="china-s")
    for kw in ({"张三": "某某"}, {"李四": "某某"}, {"账户": "AC"}, {"Bank": "Bk"}):
        pdf = mask_pdf(pdf, kw)
    t = page_text(pdf)
    assert "客户" in t and "张三" not in t and "李四" not in t and "账户" not in t


def test_digits_in_cjk_font_match_first_time():  # T3
    t = page_text(mask_pdf(make_pdf(["客户：张三 账户 123456"], fontname="china-s"), {"123456": "******"}))
    assert "1" not in t.replace(" ", "") or "123456" not in t.replace(" ", "")
    assert "*" in t


def test_two_passes_equal_one_pass():  # T4
    pdf = make_pdf(["Client Zhang San Account ACME Holdings"])
    two = page_text(mask_pdf(mask_pdf(pdf, {"Zhang San": "XX"}), {"ACME": "YY"}))
    one = page_text(mask_pdf(pdf, {"Zhang San": "XX", "ACME": "YY"}))
    assert two.split() == one.split()


def test_untouched_spans_keep_font():  # T5
    pdf = make_pdf(["Client Zhang San", ("Other line stays", 200)])
    before = [(s["font"], s["size"]) for s in spans(pdf) if "Other" in s["text"]]
    after = [(s["font"], s["size"]) for s in spans(mask_pdf(pdf, {"Zhang San": "X"})) if "Other" in s["text"]]
    assert before == after


def test_overlapping_keywords_longest_wins():  # T9
    t = page_text(mask_pdf(make_pdf(["Ann Annabelle Ann"]), {"Ann": "A1", "Annabelle": "B2"}))
    # 替换文字是单独画上去的对象，抽取顺序不保证，按数量断言
    assert sorted(t.split()) == ["A1", "A1", "B2"]


def test_target_not_masked_again():  # T10
    t = page_text(mask_pdf(make_pdf(["Zhang wrote"]), {"Zhang": "Li Si", "Li Si": "***"}))
    assert "Li Si" in t and "***" not in t


def test_short_compact_keyword_does_not_ignore_whitespace():  # T11
    t = page_text(mask_pdf(make_pdf(["AB and A B"]), {"A B": "X"}))
    assert t.split()[0] == "AB"


def test_scanned_page_rejected_even_with_text_cover():  # T13
    doc = fitz.open()
    doc.new_page().insert_text((50, 80), "Cover page")
    p2 = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), 0)
    p2.insert_image(p2.rect, pixmap=pix)
    logo = doc.new_page()
    logo.insert_image(fitz.Rect(10, 10, 60, 60), pixmap=pix)
    with pytest.raises(ScannedPageError) as e:
        mask_pdf(doc.tobytes(), {"Cover": "X"})
    assert e.value.pages == [2]


def test_invalid_pdf():
    with pytest.raises(InvalidPdfError):
        mask_pdf(b"%PDF-1.7 garbage", {"abc": "x"})


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_rotated_pages(rot):  # T15
    t = page_text(mask_pdf(make_pdf(["Client Zhang San Account"], rotate=rot), {"Zhang San": ""}))
    assert "Zhang" not in t and "Client" in t and "Account" in t


def test_unicode_case_folding():  # T16
    assert fold("K") == fold("k")
    assert fold("Ж") == fold("ж")
    assert fold("ß") != "ss"


def test_vector_kept_image_pixels_blanked():  # T12
    doc = fitz.open(); p = doc.new_page()
    p.draw_line((40, 76), (400, 76), color=(0, 0, 1), width=0.5)
    p.draw_rect(fitz.Rect(90, 70, 110, 80), color=(0, 1, 0), width=0.5)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), 0); pix.set_rect(pix.irect, (200, 0, 0))
    p.insert_image(fitz.Rect(115, 60, 155, 100), pixmap=pix)
    p.insert_text((50, 80), "Client Zhang San Account", fontsize=11)
    out = mask_pdf(doc.tobytes(), {"Zhang San": ""})
    with fitz.open(stream=out, filetype="pdf") as d:
        page = d[0]
        colors = {x.get("color") for x in page.get_drawings()}
        assert (0.0, 0.0, 1.0) in colors and (0.0, 1.0, 0.0) in colors
        img = fitz.Pixmap(d, page.get_images()[0][0])
        red = sum(1 for i in range(0, len(img.samples), img.n) if img.samples[i] > 150 and img.samples[i + 1] < 80)
        assert 0 < red < 1600


def test_long_target_falls_back_to_solid_fill(caplog):  # T14
    out = mask_pdf(make_pdf(["Client Zhang San Account"]), {"Zhang": "X" * 200})
    t = page_text(out)
    assert "Zhang" not in t and "XXXX" not in t
    assert "does not fit" in caplog.text and "Zhang" not in caplog.text


def test_choose_font():
    assert choose_font("café", "Helvetica") == "helv"
    assert choose_font("中文", "SimSun") == "china-ss"
    assert choose_font("（）１２３", "Other") == "china-s"


def test_engine_writes_no_temp_files(monkeypatch, tmp_path):  # T19
    import tempfile
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    monkeypatch.setattr(tempfile, "tempdir", str(readonly))
    monkeypatch.setenv("TMPDIR", str(readonly))
    assert "X" in page_text(mask_pdf(make_pdf(["Client Zhang San"]), {"Zhang San": "X"}))


def test_keyword_across_spans():  # T6
    doc = fitz.open(); p = doc.new_page()
    p.insert_text((50, 80), "Client Zhang", fontname="helv", fontsize=11)
    x = 50 + fitz.get_text_length("Client Zhang ", fontname="helv", fontsize=11)
    p.insert_text((x, 80), "San Account", fontname="tiro", fontsize=11)   # 换字体 → 另一个 span
    t = page_text(mask_pdf(doc.tobytes(), {"Zhang San": "XX"}))
    assert "Zhang" not in t and "San" not in t
    assert t.count("XX") == 1 and "Client" in t and "Account" in t


@pytest.mark.parametrize("text", ["AZhang SanB", "x Zhang San y"])
def test_adjacent_characters_survive(text):  # T17
    t = page_text(mask_pdf(make_pdf([text], fontname="helv"), {"Zhang San": ""})).replace(" ", "").replace("\n", "")
    assert "Zhang" not in t
    assert t == text.replace("Zhang San", "").replace(" ", "")


def test_italic_neighbours_survive():  # T17
    t = page_text(mask_pdf(make_pdf(["Client Zhang San Account"], fontname="Helvetica-Oblique"), {"Zhang San": ""}))
    assert "Client" in t and "Account" in t and "Zhang" not in t


def test_only_overlapping_links_removed():  # T18
    doc = fitz.open(); p = doc.new_page()
    p.insert_text((50, 80), "Client Zhang San Account", fontsize=11)
    hit = p.search_for("Zhang San")[0]
    p.insert_link({"kind": fitz.LINK_URI, "from": hit, "uri": "https://example.com/zhang"})
    p.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(50, 200, 150, 220), "uri": "https://example.com/other"})
    out = mask_pdf(doc.tobytes(), {"Zhang San": ""})
    with fitz.open(stream=out, filetype="pdf") as d:
        uris = [link["uri"] for link in d[0].get_links()]
    assert uris == ["https://example.com/other"]


@pytest.mark.parametrize("keyword,expected", [
    ("张三", 2), ("张 三", 2), ("欧阳娜娜", 2),   # 纯中文：最少 2 个字
    ("Li", 3), ("12", 3), ("张a", 3), ("张三1", 3), ("A B", 3),  # 含英文、数字、符号：仍为 3
])
def test_min_keyword_length_cjk_only_relaxed(keyword, expected):
    assert min_keyword_length(keyword) == expected


def test_two_char_chinese_name_matches_even_when_letter_spaced():
    # 同一行里汉字之间被抽出空格（「张 三」）；跨行不在支持范围内（设计 §5.2 第 9 条）
    pdf = make_pdf(["客户：张 三 账户"], fontname="china-s")
    t = page_text(mask_pdf(pdf, {"张三": "某某"}))
    assert "张" not in t and "三" not in t and "某某" in t and "账户" in t


# ---- LOOK-6772: mask_pdf_with_stats 返回实际涂抹的命中数（计费「未命中不计费」的依据） ----

def two_page_pdf(first, second):
    doc = fitz.open()
    for text in (first, second):
        doc.new_page(width=595, height=842).insert_text((50, 80), text, fontname="helv", fontsize=11)
    return doc.tobytes()


def test_stats_counts_each_hit():
    result = mask_pdf_with_stats(make_pdf(["Zhang San and ZHANG SAN"]), {"Zhang San": "X"})
    assert result.match_count == 2


def test_stats_zero_when_nothing_matches():
    result = mask_pdf_with_stats(make_pdf(["Client Zhang San Account"]), {"Nobody Here": "X"})
    assert result.match_count == 0


def test_stats_counts_hits_across_pages():
    result = mask_pdf_with_stats(two_page_pdf("Client Zhang San", "Zhang San again"), {"Zhang San": "X"})
    assert result.match_count == 2


def test_overlapping_rules_count_once():
    # 长规则先占位，短规则不会在同一处再计一次
    result = mask_pdf_with_stats(make_pdf(["Ann Annabelle Ann"]), {"Ann": "A1", "Annabelle": "B2"})
    assert result.match_count == 3


def test_mask_pdf_output_matches_stats_output():
    # 每次保存都会写随机 /ID，字节不可比；比较打码后的文字
    src = make_pdf(["Client Zhang San Account"])
    plain = page_text(mask_pdf(src, {"Zhang San": "X"}))
    with_stats = page_text(mask_pdf_with_stats(src, {"Zhang San": "X"}).pdf_bytes)
    assert plain == with_stats and "Zhang" not in plain
