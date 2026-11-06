"""PDF 打码引擎：逐字符定位关键词，只涂抹命中的字符，替换文字只画在命中矩形内。"""
from __future__ import annotations

import logging
import statistics
import unicodedata
from dataclasses import dataclass

import fitz  # PyMuPDF

log = logging.getLogger(__name__)

MIN_KEYWORD_LENGTH = 3          # 与 ginkgo MaskerKeywordNormalizer D20 一致
MIN_CJK_KEYWORD_LENGTH = 2      # 纯中文关键词放宽到 2（两字姓名），见 min_keyword_length
SCAN_IMAGE_COVERAGE = 0.5       # 无文字且图片覆盖超过半页 → 扫描页
MIN_FONT_SIZE = 4.0             # 替换文字小于这个字号就退回纯色遮罩
SAMPLE_BAND = 2.0               # 背景色采样带宽度（pt）


class MaskError(Exception):
    """打码失败的基类，message 可以直接展示给用户（不含关键词）。"""


class InvalidPdfError(MaskError):
    def __init__(self) -> None:
        super().__init__("The file is encrypted or damaged.")


class TooManyPagesError(MaskError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"The file has more than {limit} pages.")


class ScannedPageError(MaskError):
    def __init__(self, pages: list[int]) -> None:
        self.pages = pages
        joined = ", ".join(str(p) for p in pages)
        super().__init__(f"Page {joined} is a scanned image with no text layer and cannot be masked.")


@dataclass(frozen=True)
class Rule:
    source: str
    target: str
    needle: str              # 折叠后的匹配串
    ignore_whitespace: bool  # True：去空白匹配；False：连续空白视为一个空格


@dataclass
class Char:
    c: str
    rect: fitz.Rect
    span: dict


def fold(c: str) -> str:
    """逐字符简单大小写折叠，语义对齐 Java CASE_INSENSITIVE | UNICODE_CASE：不做 ß→ss 这类一对多展开。"""
    lo = c.lower()
    if len(lo) != 1:
        return c
    up = lo.upper()
    return up.lower() if len(up) == 1 else lo


def _is_han(c: str) -> bool:
    return unicodedata.name(c, "").startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH"))


def min_keyword_length(keyword: str) -> int:
    """关键词的最小长度：纯汉字（忽略空白）为 2，其余为 3。

    英文、数字只有两个字符时，子串匹配几乎每页都会命中（Li → Client、Limited），
    汉字两字组合则少见得多，所以只对纯中文放宽，方便配置两字姓名。
    """
    compact = "".join(ch for ch in keyword if not ch.isspace())
    if compact and all(_is_han(ch) for ch in compact):
        return MIN_CJK_KEYWORD_LENGTH
    return MIN_KEYWORD_LENGTH


def build_rules(keywords: dict[str, str]) -> list[Rule]:
    rules = []
    for source, target in keywords.items():
        source = source.strip()
        if not source:
            continue
        compact = "".join(ch for ch in source if not ch.isspace())
        ignore_ws = len(compact) >= min_keyword_length(compact)
        if ignore_ws:
            needle = "".join(fold(ch) for ch in compact)
        else:
            needle = " ".join("".join(fold(ch) for ch in source).split())
        rules.append(Rule(source, target or "", needle, ignore_ws))
    # 原始长度降序，稳定排序保留传入顺序（ginkgo D16：CHAR_LENGTH DESC, id ASC）
    rules.sort(key=lambda r: len(r.source), reverse=True)
    return rules


def is_latin1_encodable(text: str) -> bool:
    try:
        (text or "").encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


def choose_font(draw_text: str, src_font: str) -> str:
    """LOOK-5884：按实际要画的文字能否被 helv 编码选字体。"""
    if is_latin1_encodable(draw_text):
        return "helv"
    return "china-ss" if src_font.lower() == "simsun" else "china-s"


def span_color_to_rgb(color: int) -> tuple[float, float, float]:
    return ((color >> 16 & 255) / 255, (color >> 8 & 255) / 255, (color & 255) / 255)


def find_scanned_pages(doc: fitz.Document) -> list[int]:
    pages = []
    for page in doc:
        if page.get_text("text").strip():
            continue
        page_area = abs(page.rect)
        covered = sum(abs(fitz.Rect(info["bbox"]) & page.rect) for info in page.get_image_info())
        if page_area and covered / page_area > SCAN_IMAGE_COVERAGE:
            pages.append(page.number + 1)
    return pages


# 连字（如 ﬁ）拆成单字，否则跨连字的值只能匹配到一半，前缀明文残留
_RAWDICT_FLAGS = fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_LIGATURES


def page_lines(page: fitz.Page) -> list[list[Char]]:
    lines = []
    for block in page.get_text("rawdict", flags=_RAWDICT_FLAGS)["blocks"]:
        for line in block.get("lines", []):
            chars = [Char(ch["c"], fitz.Rect(ch["bbox"]), span)
                     for span in line["spans"] for ch in span["chars"]]
            if chars:
                lines.append(chars)
    return lines


@dataclass
class PageText:
    text: str                             # 各行逐字符拼接，行间用 \n 连接
    lines: list[list[Char]]               # page_lines(page)
    pos: list[tuple[int, int] | None]     # text 每个码点 → (行, 字符下标)；行间的 \n 为 None


def page_text(page: fitz.Page) -> PageText:
    lines = page_lines(page)
    text: list[str] = []
    pos: list[tuple[int, int] | None] = []
    for n, chars in enumerate(lines):
        if n:
            text.append("\n")
            pos.append(None)
        for k, ch in enumerate(chars):
            text.append(ch.c)
            pos.extend([(n, k)] * len(ch.c))
    return PageText("".join(text), lines, pos)


def offsets_to_parts(pt: PageText, start: int, end: int) -> list[tuple[list[Char], list[int]]]:
    """整页文本的码点区间 → 按行分组的（行字符, 命中字符下标）；跳过空白字符，同 find_hits。"""
    parts: dict[int, list[int]] = {}
    for p in pt.pos[start:end]:
        if p is None or pt.lines[p[0]][p[1]].c.isspace():
            continue
        idx = parts.setdefault(p[0], [])
        if not idx or idx[-1] != p[1]:
            idx.append(p[1])
    return [(pt.lines[n], idx) for n, idx in parts.items()]


def _haystack(chars: list[Char], ignore_ws: bool) -> tuple[str, list[int]]:
    """返回（折叠后的串, 串中每个位置对应的字符下标）。"""
    text, index = [], []
    prev_space = True
    for i, ch in enumerate(chars):
        if ch.c.isspace():
            if ignore_ws or prev_space:
                continue
            text.append(" ")
            index.append(i)
            prev_space = True
        else:
            text.append(fold(ch.c))
            index.append(i)
            prev_space = False
    return "".join(text), index


def find_hits(chars: list[Char], rules: list[Rule]) -> list[tuple[list[int], Rule]]:
    """只用到每个字符的 .c，PDF 行与普通文本（text.py）共用。"""
    occupied = [False] * len(chars)
    hits = []
    for rule in rules:
        hay, index = _haystack(chars, rule.ignore_whitespace)
        start = hay.find(rule.needle)
        while start != -1:
            first, last = index[start], index[start + len(rule.needle) - 1]
            if not any(occupied[first:last + 1]):
                for i in range(first, last + 1):
                    occupied[i] = True
                hits.append(([i for i in range(first, last + 1) if not chars[i].c.isspace()], rule))
                start = hay.find(rule.needle, start + len(rule.needle))
            else:
                start = hay.find(rule.needle, start + 1)
    return hits


match_line = find_hits


def segments(chars: list[Char], idx: list[int]) -> list[tuple[fitz.Rect, dict]]:
    """把命中字符按所在 span 切段，每段一个外接矩形。"""
    out: list[tuple[fitz.Rect, dict]] = []
    for i in idx:
        ch = chars[i]
        if out and out[-1][1] is ch.span:
            out[-1][0].include_rect(ch.rect)
        else:
            out.append((fitz.Rect(ch.rect), ch.span))
    return out


def sample_background(pix: fitz.Pixmap, matrix: fitz.Matrix, rect: fitz.Rect) -> tuple[float, float, float]:
    """在命中矩形四周的窄带上取像素，按通道取中位数。pix 是未加任何遮罩时渲染的整页。"""
    r = rect * matrix  # 未旋转坐标 → 渲染后的像素坐标
    bands = [
        fitz.Rect(r.x0 - SAMPLE_BAND, r.y0, r.x0, r.y1),
        fitz.Rect(r.x1, r.y0, r.x1 + SAMPLE_BAND, r.y1),
        fitz.Rect(r.x0, r.y0 - SAMPLE_BAND, r.x1, r.y0),
        fitz.Rect(r.x0, r.y1, r.x1, r.y1 + SAMPLE_BAND),
    ]
    channels: list[list[int]] = [[], [], []]
    for band in bands:
        band = band.round() & pix.irect
        if band.is_empty:
            continue
        for y in range(band.y0, band.y1):
            for x in range(band.x0, band.x1):
                px = pix.pixel(x, y)
                for k in range(3):
                    channels[k].append(px[k])
    if not channels[0]:
        return (1.0, 1.0, 1.0)
    return tuple(statistics.median(ch) / 255 for ch in channels)


def fit_font_size(text: str, fontname: str, size: float, rect: fitz.Rect) -> float:
    width = fitz.get_text_length(text, fontname=fontname, fontsize=size)
    if width > rect.width:
        size = size * rect.width / width
    return min(size, rect.height / 1.2)


@dataclass(frozen=True)
class Replacement:
    parts: list[tuple[list[Char], list[int]]]  # 每行一段：（行字符, 命中字符下标）
    text: str                                   # 替换文字，只画在第一段；空串 = 只涂底色


def replace_spans(page: fitz.Page, reps: list[Replacement]) -> list[list[fitz.Rect]]:
    """涂掉每个 Replacement 命中的字符并写替换文字，返回每个 Replacement 涂抹的矩形（按 span 切段）。"""
    if not reps:
        return []
    pix = page.get_pixmap(annots=False)
    matrix = page.rotation_matrix
    out = []
    for rep in reps:
        segs = [seg for chars, idx in rep.parts for seg in segments(chars, idx)]
        for n, (rect, span) in enumerate(segs):
            fill = sample_background(pix, matrix, rect)
            text = rep.text if n == 0 else ""
            if not text:
                page.add_redact_annot(rect, fill=fill)
                continue
            fontname = choose_font(text, span["font"])
            size = fit_font_size(text, fontname, span["size"], rect)
            if size < MIN_FONT_SIZE:
                log.warning("replacement text does not fit, falling back to solid fill (page=%s)", page.number + 1)
                page.add_redact_annot(rect, fill=fill)
                continue
            page.add_redact_annot(rect, text=text, fontname=fontname, fontsize=size,
                                  text_color=span_color_to_rgb(span["color"]), fill=fill)
        out.append([fitz.Rect(rect) for rect, _ in segs])
    page.apply_redactions(images=2, graphics=0, text=0)
    return out


def mask_page(page: fitz.Page, rules: list[Rule]) -> int:
    hits = [Replacement([(chars, idx)], rule.target)
            for chars in page_lines(page) for idx, rule in find_hits(chars, rules)]
    if not hits:
        return 0
    replace_spans(page, hits)
    return len(hits)


@dataclass(frozen=True)
class MaskResult:
    pdf_bytes: bytes
    match_count: int  # 实际涂抹的命中数（各页 mask_page() 之和）；CT 据此判断「未命中不计费」（LOOK-6772）


def mask_pdf_with_stats(pdf_bytes: bytes, keywords: dict[str, str], max_pages: int | None = None) -> MaskResult:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise InvalidPdfError() from e
    try:
        if doc.needs_pass:
            raise InvalidPdfError()
        if max_pages is not None and doc.page_count > max_pages:
            raise TooManyPagesError(max_pages)
        scanned = find_scanned_pages(doc)
        if scanned:
            raise ScannedPageError(scanned)
        rules = build_rules(keywords)
        hits = sum(mask_page(page, rules) for page in doc)
        return MaskResult(doc.tobytes(garbage=4, clean=True, deflate=True), hits)
    finally:
        doc.close()


def mask_pdf(pdf_bytes: bytes, keywords: dict[str, str], max_pages: int | None = None) -> bytes:
    return mask_pdf_with_stats(pdf_bytes, keywords, max_pages).pdf_bytes
