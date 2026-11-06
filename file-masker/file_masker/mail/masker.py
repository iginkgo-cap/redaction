"""整封邮件打码：标题、发件人、正文、附件名、PDF（含 zip 内）、文本附件全部打码，再整体扫一遍残留。
打不了码的附件跳过（记在 skipped），其余照常输出；标题正文处理不了或仍有残留，整封扣留。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import fitz
from bs4 import BeautifulSoup, Comment, Doctype

from file_masker import engine
from file_masker.engine import Rule, build_rules, find_hits, page_lines
from file_masker.mail.archive import ArchiveError, expand_zip
from file_masker.mail.mime import Attachment, MaskedMail, MimeDecodeError, parse
from file_masker.text import find_residuals, mask_segments, mask_text

MAX_OUTGOING_BYTES = 20 * 1024 * 1024
MAX_PAGES = 2000
# 图片与外部资源一律去掉：内容无法打码（data: 内嵌图），或会外联（远程图、跟踪像素）
_REMOVED_TAGS = ["script", "img", "picture", "svg", "object", "embed", "iframe", "video", "audio", "source", "link"]
_CSS_URL = re.compile(r"url\s*\([^)]*\)", re.IGNORECASE)


@dataclass(frozen=True)
class Held:
    reason: str               # 原因码，页面上展示
    masked_subject: str = ""  # 只有打码后的标题，不含原文


class _Hold(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def mask_mail(raw: bytes, keywords: dict[str, str], max_bytes: int = MAX_OUTGOING_BYTES) -> MaskedMail | Held:
    rules = build_rules(keywords)
    try:
        parsed = parse(raw)
    except MimeDecodeError:
        return Held("decode_error")
    subject, hits = mask_text(parsed.subject, rules)
    try:
        original_from, n = mask_text(parsed.from_, rules)
        hits += n
        text_body = None
        if parsed.text is not None:
            text_body, n = mask_text(parsed.text, rules)
            hits += n
        html_body, html_images = None, 0
        if parsed.html is not None:
            html_body, n, html_images = _mask_html(parsed.html, rules)
            hits += n
        files, removed_images, unmaskable = _collect_files(parsed.attachments)
        removed_images += html_images
        attachments, skipped, used = [], [], set()
        for name, kind, data in files:
            masked_name, n = mask_text(name, rules)
            hits += n
            try:
                masked_data, m = _mask_txt(data, rules) if kind == "text" else _mask_pdf(data, keywords)
            except _Hold as h:
                skipped.append((masked_name, h.reason))
                continue
            hits += m
            attachments.append((_unique_name(masked_name, f".{'txt' if kind == 'text' else 'pdf'}", used), masked_data))
        for name, reason in unmaskable:
            masked_name, n = mask_text(name, rules)
            hits += n
            skipped.append((masked_name, reason))
        if text_body is None and html_body is not None:
            text_body = _html_to_text(html_body)  # 原邮件只有 HTML：从打码后的 HTML 抽纯文本，body_text 一定有（设计 §4.4）
        result = MaskedMail(subject, original_from, parsed.date, text_body, html_body, attachments, removed_images, hits,
                            message_id=parsed.message_id, original_datetime=parsed.date_parsed, skipped=skipped)
        if _has_residuals(result, rules):
            raise _Hold("residual_keyword")
        if _estimated_size(result) > max_bytes:
            raise _Hold("too_large")
        return result
    except _Hold as h:
        return Held(h.reason, subject)


def _collect_files(attachments: list[Attachment]) -> tuple[list[tuple[str, str, bytes]], int, list[tuple[str, str]]]:
    """返回（[(名字, pdf/text, 内容)], 丢掉的图片数, [(名字, 原因)] 打不了码的附件）。"""
    files, images, unmaskable = [], 0, []
    for a in attachments:
        if a.kind == "other":
            unmaskable.append((a.name or "attachment", "unsupported_attachment"))
        elif a.kind == "image":
            images += 1  # 签名 logo 等图片直接丢弃，不转发
        elif a.kind == "signature":
            continue
        elif a.kind == "pdf":
            files.append((a.name or "attachment.pdf", "pdf", a.data))
        elif a.kind == "text":
            files.append((a.name or "attachment.txt", "text", a.data))
        else:
            try:
                entries = expand_zip(a.data)
            except ArchiveError:
                unmaskable.append((a.name or "archive.zip", "bad_archive"))
                continue
            stem = os.path.splitext(a.name or "archive.zip")[0]
            files += [(f"{stem}__{name}", "pdf", data) for name, data in entries]
    return files, images, unmaskable


def _mask_pdf(data: bytes, keywords: dict[str, str]) -> tuple[bytes, int]:
    try:
        result = engine.mask_pdf_with_stats(data, keywords, MAX_PAGES)
    except engine.ScannedPageError as e:
        raise _Hold("scanned_pdf") from e
    except engine.TooManyPagesError as e:
        raise _Hold("too_large") from e
    except engine.MaskError as e:
        raise _Hold("invalid_pdf") from e
    return result.pdf_bytes, result.match_count


def _mask_txt(data: bytes, rules: list[Rule]) -> tuple[bytes, int]:
    text, hits = mask_text(data.decode("utf-8"), rules)
    return text.encode("utf-8"), hits


def _mask_html(html: str, rules: list[Rule]) -> tuple[str, int, int]:
    """返回（打码后的 HTML, 命中数, 去掉的非 cid 图片数）。
    去掉脚本、注释、图片与外部资源、CSS 里的 url()；所有文本节点连起来匹配，
    所以被标签拆开的关键词（Outlook 的 <span>Zhang</span><span> San</span>）也能打掉；属性值逐个打码。"""
    soup = BeautifulSoup(html, "html.parser")
    images = sum(1 for img in soup.find_all("img") if not str(img.get("src", "")).strip().lower().startswith("cid:"))
    for tag in soup.find_all(_REMOVED_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(True):
        tag.attrs.pop("background", None)
        if "style" in tag.attrs:
            tag["style"] = _CSS_URL.sub("none", _attr_text(tag["style"]))
    for style in soup.find_all("style"):
        for node in style.find_all(string=True):
            _replace_text(node, _CSS_URL.sub("none", str(node)))

    nodes = [n for n in soup.find_all(string=True) if not isinstance(n, Doctype)]
    masked, hits = mask_segments([str(n) for n in nodes], rules)
    for node, text in zip(nodes, masked):
        _replace_text(node, text)
    for tag in soup.find_all(True):
        for name, value in list(tag.attrs.items()):
            out, n = mask_text(_attr_text(value), rules)
            if n:
                tag[name] = out
                hits += n
    return str(soup), hits, images


def _html_to_text(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text("\n").strip()


def _replace_text(node, text: str) -> None:
    if text != str(node):
        node.replace_with(text)


def _attr_text(value: object) -> str:
    return " ".join(value) if isinstance(value, list) else str(value)  # class 这类多值属性是 list


def _has_residuals(mail: MaskedMail, rules: list[Rule]) -> bool:
    texts = [mail.subject, mail.original_from, mail.text_body or ""]
    texts += [name for name, _ in mail.attachments] + [name for name, _ in mail.skipped]
    if mail.html_body is not None:
        soup = BeautifulSoup(mail.html_body, "html.parser")
        texts.append(soup.get_text(" "))  # 拼接后的可见文本，能发现 <b>Zhang</b> San 这种被拆开的
        texts += [_attr_text(v) for tag in soup.find_all(True) for v in tag.attrs.values()]
    if any(find_residuals(t, rules) for t in texts):
        return True
    return any(find_residuals(data.decode("utf-8"), rules) if name.lower().endswith(".txt") else _pdf_residuals(data, rules)
               for name, data in mail.attachments)


def _pdf_residuals(data: bytes, rules: list[Rule]) -> int:
    with fitz.open(stream=data, filetype="pdf") as doc:
        return sum(len(find_hits(chars, rules)) for page in doc for chars in page_lines(page))


def _unique_name(name: str, want_ext: str, used: set[str]) -> str:
    """扩展名不是 want_ext 时补上：发送和残留检查都按扩展名区分 PDF 与文本附件。"""
    stem, ext = os.path.splitext(name.strip())
    if ext.lower() != want_ext:
        stem, ext = name.strip(), want_ext
    stem = stem or "attachment"
    candidate, i = f"{stem}{ext}", 1
    while candidate.lower() in used:
        candidate = f"{stem} ({i}){ext}"
        i += 1
    used.add(candidate.lower())
    return candidate


def _estimated_size(mail: MaskedMail) -> int:
    size = sum(len(data) for _, data in mail.attachments)
    size += len((mail.text_body or "").encode()) + len((mail.html_body or "").encode())
    return size * 4 // 3  # base64 编码后的大小
