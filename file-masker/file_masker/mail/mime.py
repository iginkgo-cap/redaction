"""邮件解析与组装。解析：把原邮件拆成标题 / 正文 / 分好类的附件；组装：用打码结果新建一封邮件（不是转发）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.errors import HeaderParseError
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from html import escape

OWN_HEADER = "X-File-Masker"
_CHARSET_ALIAS = {"gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030"}
_ENCODED_WORD = re.compile(r"=\?[^?]+\?[bq]\?", re.IGNORECASE)
_MESSAGE_ID = re.compile(r"^<[^<>\s]+>$")  # 非 <…> 形式一律视为缺失（E16）
_ZIP_TYPES = {"application/zip", "application/x-zip-compressed"}
_SIGNATURE_TYPES = {"application/pkcs7-signature", "application/x-pkcs7-signature"}
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")


class MimeDecodeError(Exception):
    pass


@dataclass
class Attachment:
    name: str | None
    kind: str  # pdf / zip / image / text / signature / other
    data: bytes  # text 为解码后的 UTF-8


@dataclass
class ParsedMail:
    subject: str
    from_: str
    date: str
    text: str | None
    html: str | None
    attachments: list[Attachment] = field(default_factory=list)
    message_id: str | None = None          # 原 Message-ID 头原样；缺失或非法为 None
    date_parsed: datetime | None = None    # 原 Date 头解析结果；不可解析为 None


@dataclass
class MaskedMail:
    subject: str
    original_from: str
    original_date: str
    text_body: str | None
    html_body: str | None
    attachments: list[tuple[str, bytes]]
    removed_images: int
    hits: int
    message_id: str | None = None
    original_datetime: datetime | None = None
    skipped: list[tuple[str, str]] = field(default_factory=list)  # 打不了码、没带上的附件：(打码后的名字, 原因)


def _header(msg: EmailMessage, name: str) -> str:
    value = str(msg.get(name) or "")
    if _ENCODED_WORD.search(value):  # 未知字符集的 encoded-word 解不开，原样留着就漏过了关键词匹配
        raise MimeDecodeError(f"cannot decode {name}")
    return value


def _leaves(part: EmailMessage):
    if part.get_content_type() == "message/rfc822":
        yield part  # 附带的整封邮件当作不支持的附件，不往里拆
    elif part.is_multipart():
        for sub in part.iter_parts():
            yield from _leaves(sub)
    else:
        yield part


def _decode_text(part: EmailMessage) -> str:
    charset = (part.get_content_charset() or "utf-8").lower()
    try:
        return (part.get_payload(decode=True) or b"").decode(_CHARSET_ALIAS.get(charset, charset), errors="replace")
    except LookupError as e:
        raise MimeDecodeError(f"unknown charset {charset}") from e


def _message_id(msg: EmailMessage) -> str | None:
    try:
        value = str(msg.get("Message-ID") or "").strip()
    except (HeaderParseError, IndexError, ValueError):  # 畸形值（如 "<>"）在读取头时就会抛错
        return None
    return value if _MESSAGE_ID.match(value) else None


def _date_parsed(msg: EmailMessage) -> datetime | None:
    value = str(msg.get("Date") or "")
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def _kind(content_type: str, name: str | None) -> str:
    lower = (name or "").lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        return "pdf"
    if content_type in _ZIP_TYPES or lower.endswith(".zip"):
        return "zip"
    if content_type.startswith("image/") or lower.endswith(_IMAGE_EXTS):
        return "image"  # 有的发件系统把内嵌图声明为 octet-stream，按扩展名认
    if content_type == "text/plain":
        return "text"
    if content_type in _SIGNATURE_TYPES:
        return "signature"  # S/MIME 签名：丢弃即可，不含正文
    return "other"


def parse(raw: bytes) -> ParsedMail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    texts, htmls, attachments = [], [], []
    for part in _leaves(msg):
        content_type = part.get_content_type()
        name = part.get_filename()
        is_body = name is None and part.get_content_disposition() != "attachment"
        if is_body and content_type == "text/plain":
            texts.append(_decode_text(part))
        elif is_body and content_type == "text/html":
            htmls.append(_decode_text(part))
        else:
            kind = _kind(content_type, name)
            if kind == "text":
                data = _decode_text(part).encode("utf-8")
            else:
                data = b"" if content_type == "message/rfc822" else (part.get_payload(decode=True) or b"")
            attachments.append(Attachment(name, kind, data))
    return ParsedMail(
        subject=_header(msg, "Subject"),
        from_=_header(msg, "From"),
        date=str(msg.get("Date") or ""),
        text="\n".join(texts) if texts else None,
        html="\n".join(htmls) if htmls else None,
        attachments=attachments,
        message_id=_message_id(msg),
        date_parsed=_date_parsed(msg),
    )


def is_own_output(raw: bytes) -> bool:
    return BytesHeaderParser(policy=policy.default).parsebytes(raw).get(OWN_HEADER) is not None


def _one_line(value: str) -> str:
    return " ".join(value.split())


def build(masked: MaskedMail, sender: str, to: str, version: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = _one_line(masked.subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="file-masker")
    msg[OWN_HEADER] = version
    if masked.original_date:
        msg["X-Original-Date"] = _one_line(masked.original_date)
    if masked.original_from:
        msg["X-Original-From"] = _one_line(masked.original_from)

    footer = f"Masked by file-masker {version}."
    if masked.removed_images:
        footer += f" {masked.removed_images} image(s) removed."
    if masked.skipped:
        footer += f" {len(masked.skipped)} attachment(s) could not be masked and were left out."
    text = masked.text_body
    if text is None and masked.html_body is None:
        text = ""
    if text is not None:
        msg.set_content(f"{text}\n\n-- \n{footer}\n")
    if masked.html_body is not None:
        html = f'{masked.html_body}\n<p style="color:#888;font-size:12px">{escape(footer)}</p>'
        if text is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    for name, data in masked.attachments:
        if name.lower().endswith(".txt"):
            msg.add_attachment(data.decode("utf-8"), subtype="plain", filename=name)
        else:
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg
