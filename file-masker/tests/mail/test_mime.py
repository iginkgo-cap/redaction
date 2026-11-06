import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from conftest import make_pdf
from file_masker.mail.mime import MaskedMail, MimeDecodeError, build, is_own_output, parse

PDF = make_pdf(["hello"])


def rich_mail():
    msg = EmailMessage()
    msg["From"] = "RM Zhang <rm@bank.com>"
    msg["To"] = "client@example.com"
    msg["Cc"] = "boss@example.com"
    msg["Subject"] = "张三 9月结单"
    msg["Date"] = "Tue, 13 Oct 2026 10:00:00 +0800"
    msg.set_content("plain body")
    msg.add_alternative('<p>html body <img src="cid:logo"></p>', subtype="html")
    msg.get_payload()[1].add_related(b"\x89PNG", "image", "png", cid="<logo>")
    msg.add_attachment(PDF, maintype="application", subtype="pdf", filename="张三.pdf")
    return msg


def test_parse_multipart():
    parsed = parse(rich_mail().as_bytes())
    assert parsed.subject == "张三 9月结单"
    assert parsed.from_ == "RM Zhang <rm@bank.com>"
    assert parsed.date == "Tue, 13 Oct 2026 10:00:00 +0800"
    assert parsed.text.strip() == "plain body"
    assert "html body" in parsed.html
    assert sorted((a.kind, a.name) for a in parsed.attachments) == [("image", None), ("pdf", "张三.pdf")]
    assert next(a for a in parsed.attachments if a.kind == "pdf").data == PDF


@pytest.mark.parametrize("maintype,subtype,filename,kind", [
    ("application", "octet-stream", "s.PDF", "pdf"),
    ("application", "zip", "a.zip", "zip"),
    ("application", "x-zip-compressed", "a.zip", "zip"),
    ("application", "octet-stream", "a.zip", "zip"),
    ("image", "jpeg", "photo.jpg", "image"),
    ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet", "a.xlsx", "other"),
    ("text", "plain", "notes.txt", "text"),
    ("application", "octet-stream", "logo.PNG", "image"),  # iFAST 的内嵌图声明为 octet-stream
    ("application", "pkcs7-signature", "smime.p7s", "signature"),
    ("application", "x-pkcs7-signature", "smime.p7s", "signature"),
])
def test_attachment_kinds(maintype, subtype, filename, kind):
    msg = EmailMessage()
    msg.set_content("body")
    msg.add_attachment(b"data", maintype=maintype, subtype=subtype, filename=filename)
    assert [a.kind for a in parse(msg.as_bytes()).attachments] == [kind]


def test_text_attachment_is_decoded_to_utf8():
    msg = EmailMessage()
    msg.set_content("body")
    msg.add_attachment("免责声明".encode("gb2312"), maintype="text", subtype="plain", filename="disclaim.txt")
    msg.get_payload()[1].set_param("charset", "gb2312")
    [att] = parse(msg.as_bytes()).attachments
    assert att.data.decode("utf-8") == "免责声明"


def test_attached_email_is_other_and_not_walked_into():
    inner = rich_mail()
    msg = EmailMessage()
    msg.set_content("see attached")
    msg.add_attachment(inner)
    parsed = parse(msg.as_bytes())
    assert [a.kind for a in parsed.attachments] == ["other"]
    assert parsed.text.strip() == "see attached"


def test_gb2312_label_decoded_as_gb18030():
    body = "客户 朱镕基".encode("gb18030")  # 「镕」不在 GB2312 内
    subject = base64.b64encode("张三".encode("gb2312")).decode()
    raw = (f"Subject: =?gb2312?B?{subject}?=\r\nContent-Type: text/plain; charset=gb2312\r\n"
           "Content-Transfer-Encoding: base64\r\n\r\n").encode() + base64.b64encode(body) + b"\r\n"
    parsed = parse(raw)
    assert parsed.subject == "张三"
    assert parsed.text == "客户 朱镕基"


def test_unknown_body_charset_raises():
    raw = b"Subject: x\r\nContent-Type: text/plain; charset=x-unknown-42\r\n\r\nhello\r\n"
    with pytest.raises(MimeDecodeError):
        parse(raw)


def test_undecodable_subject_raises():
    # 未闭合的 encoded-word 会原样留下：关键词匹配只看到 base64，收件客户端却可能宽松解码显示出「张三」
    raw = b"Subject: =?utf-8?B?5byg5LiJ\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
    with pytest.raises(MimeDecodeError):
        parse(raw)


def test_is_own_output():
    msg = EmailMessage()
    msg["X-File-Masker"] = "1.2.0"
    msg.set_content("x")
    assert is_own_output(msg.as_bytes())
    assert not is_own_output(rich_mail().as_bytes())


def masked(**kw):
    base = dict(subject="CLIENT-A 9月结单", original_from="RM <rm@bank.com>", original_date="Tue, 13 Oct 2026 10:00:00 +0800",
                text_body="plain", html_body="<p>html</p>", attachments=[("CLIENT-A.pdf", PDF)], removed_images=1, hits=3)
    base.update(kw)
    return MaskedMail(**base)


def reparse(msg):
    return BytesParser(policy=policy.default).parsebytes(msg.as_bytes())


def test_build_headers_and_parts():
    out = reparse(build(masked(), sender="src@example.com", to="dst@example.com", version="1.2.0"))
    assert out["From"] == "src@example.com"
    assert out["To"] == "dst@example.com"
    assert out["Subject"] == "CLIENT-A 9月结单"
    assert out["X-File-Masker"] == "1.2.0"
    assert out["X-Original-Date"] == "Tue, 13 Oct 2026 10:00:00 +0800"
    assert out["X-Original-From"] == "RM <rm@bank.com>"
    for header in ("Cc", "Reply-To", "Received"):
        assert out[header] is None
    assert out["Message-ID"]
    text = out.get_body(("plain",)).get_content()
    html = out.get_body(("html",)).get_content()
    assert text.startswith("plain") and "1 image(s) removed" in text
    assert "<p>html</p>" in html and "1 image(s) removed" in html
    [att] = list(out.iter_attachments())
    assert att.get_filename() == "CLIENT-A.pdf" and att.get_content() == PDF


def test_build_text_attachment_as_plain_text():
    out = reparse(build(masked(attachments=[("disclaim.txt", "CLIENT-A 声明".encode())]), "a@x.com", "b@x.com", "1.2.0"))
    [att] = list(out.iter_attachments())
    assert att.get_content_type() == "text/plain"
    assert att.get_filename() == "disclaim.txt" and att.get_content().rstrip("\n") == "CLIENT-A 声明"  # set_content 会补行尾换行


def test_build_footer_mentions_skipped_attachments():
    out = reparse(build(masked(skipped=[("a.xlsx", "unsupported_attachment")]), "a@x.com", "b@x.com", "1.2.0"))
    assert "1 attachment(s) could not be masked and were left out" in out.get_body(("plain",)).get_content()


def test_build_html_only_and_no_body():
    out = reparse(build(masked(text_body=None, attachments=[], removed_images=0), "a@x.com", "b@x.com", "1.2.0"))
    assert out.get_content_type() == "text/html"
    assert "image(s) removed" not in out.get_content()
    out = reparse(build(masked(text_body=None, html_body=None, attachments=[]), "a@x.com", "b@x.com", "1.2.0"))
    assert out.get_content_type() == "text/plain"


def test_build_flattens_header_line_breaks():
    out = reparse(build(masked(subject="a\r\n b", original_from="x\n y"), "a@x.com", "b@x.com", "1.2.0"))
    assert out["Subject"] == "a b"
    assert out["X-Original-From"] == "x y"


# ---- Step 5（LOOK-6764 二期）：Message-ID、解析后的日期 ----

def test_parse_extracts_message_id_and_datetime():
    msg = rich_mail()
    msg["Message-ID"] = "<abc.123@mail.example.com>"
    parsed = parse(msg.as_bytes())
    assert parsed.message_id == "<abc.123@mail.example.com>"
    assert parsed.date_parsed.isoformat() == "2026-10-13T10:00:00+08:00"


@pytest.mark.parametrize("header", [None, "", "no-angle-brackets@x", "<has space@x>", "<>"])
def test_parse_message_id_missing_or_malformed_is_none(header):
    raw = rich_mail().as_bytes()
    if header is not None:  # 畸形值连 EmailMessage 赋值都会拒绝，直接拼进原始字节
        raw = f"Message-ID: {header}\r\n".encode() + raw
    assert parse(raw).message_id is None


def test_parse_bad_or_missing_date_is_none():
    msg = rich_mail()
    del msg["Date"]
    assert parse(msg.as_bytes()).date_parsed is None
    msg["Date"] = "yesterday-ish"
    assert parse(msg.as_bytes()).date_parsed is None
