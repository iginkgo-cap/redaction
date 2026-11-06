import io
import zipfile
from email.message import EmailMessage

import fitz
import pytest

from conftest import make_pdf, page_text
from file_masker import engine
from file_masker.mail import masker
from file_masker.mail.masker import Held, mask_mail
from file_masker.mail.mime import MaskedMail

KW = {"Zhang San": "CLIENT-A", "12345678": "ACCT-A01"}
PDF = make_pdf(["Client Zhang San", "Account 12345678"])


def mail(subject="Zhang San statement", text="Account 12345678", html=None, attachments=(), sender="RM <rm@bank.com>"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "client@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 13 Oct 2026 10:00:00 +0800"
    if text is not None:
        msg.set_content(text)
    if html is not None:
        if text is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    for name, maintype, subtype, data in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg


def pdf_att(name="Zhang San.pdf", data=PDF):
    return (name, "application", "pdf", data)


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def scanned_pdf():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), 0))
    return doc.tobytes()


def run(msg, keywords=KW, **kw):
    return mask_mail(msg.as_bytes(), keywords, **kw)


def test_masks_every_part():
    html = "<p>Dear Zhang San</p><table><tr><td>12345678</td></tr></table>"
    result = run(mail(html=html, attachments=[pdf_att()]))
    assert isinstance(result, MaskedMail)
    assert result.subject == "CLIENT-A statement"
    assert result.original_from == "RM <rm@bank.com>"
    assert result.original_date == "Tue, 13 Oct 2026 10:00:00 +0800"
    assert result.text_body.strip() == "Account ACCT-A01"
    assert "<table>" in result.html_body and "CLIENT-A" in result.html_body and "ACCT-A01" in result.html_body
    assert "Zhang" not in result.html_body and "12345678" not in result.html_body
    [(name, data)] = result.attachments
    assert name == "CLIENT-A.pdf"
    text = page_text(data)
    assert "CLIENT-A" in text and "Zhang" not in text and "12345678" not in text
    assert result.hits == 7  # 标题 1 + 纯文本 1 + HTML 2 + 文件名 1 + PDF 2


def test_sender_display_name_is_masked():
    assert run(mail(sender="Zhang San <zs@bank.com>")).original_from == "CLIENT-A <zs@bank.com>"


def test_mail_without_hits_is_still_sent():
    result = run(mail(subject="hello", text="nothing sensitive"))
    assert isinstance(result, MaskedMail) and result.hits == 0


def test_zip_is_expanded_into_masked_pdfs():
    data = zip_bytes([("a.pdf", PDF), ("b.pdf", PDF)])
    result = run(mail(attachments=[("Zhang San.zip", "application", "zip", data)]))
    assert [name for name, _ in result.attachments] == ["CLIENT-A__a.pdf", "CLIENT-A__b.pdf"]
    assert all("Zhang" not in page_text(d) for _, d in result.attachments)


def test_duplicate_attachment_names_are_made_unique():
    result = run(mail(attachments=[pdf_att("a.pdf"), pdf_att("a.pdf"), pdf_att(None)]))
    assert [name for name, _ in result.attachments] == ["a.pdf", "a (1).pdf", "attachment.pdf"]


def test_html_attributes_masked_and_unsafe_parts_removed():
    html = ('<p title="Zhang San">hi <a href="mailto:12345678@bank.com">mail</a></p>'
            "<!-- Zhang San --><script>var n='Zhang San'</script>")
    result = run(mail(text=None, html=html))
    body = result.html_body
    assert 'title="CLIENT-A"' in body and 'href="mailto:ACCT-A01@bank.com"' in body
    assert "<script" not in body and "<!--" not in body
    assert "Zhang" not in body
    # 设计 §4.4：原邮件只有 HTML 时，从打码后的 HTML 抽纯文本，body_text 一定有（此例关键词只在属性里，可见文本无关键词）
    assert result.text_body is not None and "<" not in result.text_body and "mail" in result.text_body


def test_masked_mail_carries_message_id_and_datetime():
    msg = mail()
    msg["Message-ID"] = "<id-1@bank.com>"
    result = run(msg)
    assert result.message_id == "<id-1@bank.com>"
    assert result.original_datetime.isoformat() == "2026-10-13T10:00:00+08:00"
    assert result.original_date == "Tue, 13 Oct 2026 10:00:00 +0800"


def test_every_html_image_and_css_url_is_removed():
    # data: 内嵌图、远程图（跟踪像素）、背景图都可能带敏感内容或外联，一律去掉
    html = ('<p>a<img src="data:image/png;base64,AAAA"><img src="https://x/o.gif?id=99"></p>'
            '<table background="https://x/bg.png"><tr><td style="color:red;background-image:url(data:image/png;base64,BBB)">'
            "x</td></tr></table><style>.c{background:url('https://x/c.png')}</style>")
    result = run(mail(text=None, html=html))
    body = result.html_body
    assert "<img" not in body and "data:image" not in body and "https://x" not in body and "url(" not in body
    assert "color:red" in body
    assert result.removed_images == 2


def test_tag_split_keyword_is_masked():
    # Outlook 常把名字拆进相邻 <span>
    result = run(mail(text=None, html='<p><span lang="EN-US">Zhang</span><span lang="EN-US"> San</span>, hi</p>'))
    assert isinstance(result, MaskedMail)
    assert "CLIENT-A" in result.html_body and "Zhang" not in result.html_body and "San<" not in result.html_body
    assert "CLIENT-A" in result.text_body and "Zhang" not in result.text_body  # HTML-only：纯文本从打码后的 HTML 抽取


def test_html_residual_is_held(monkeypatch):
    monkeypatch.setattr(masker, "_mask_html", lambda html, rules: (html, 0, 0))  # 模拟打码漏掉
    assert run(mail(text=None, html="<p>Zhang San</p>")) == Held("residual_keyword", "CLIENT-A statement")


def test_smime_signature_is_dropped():
    msg = mail(attachments=[pdf_att(), ("smime.p7s", "application", "pkcs7-signature", b"sig")])
    result = run(msg)
    assert isinstance(result, MaskedMail)
    assert [name for name, _ in result.attachments] == ["CLIENT-A.pdf"]


def test_html_entities_are_decoded_before_matching():
    result = run(mail(text=None, html="<p>&#90;hang San</p>"))
    assert "CLIENT-A" in result.html_body and "hang San" not in result.html_body


def test_images_are_dropped_and_counted():
    msg = mail(text="body", html='<p>x<img src="cid:a"><img src="cid:b"></p>',
               attachments=[("photo.jpg", "image", "jpeg", b"\xff\xd8")])
    html_part = msg.get_payload()[0].get_payload()[1]
    html_part.add_related(b"\x89PNG", "image", "png", cid="<a>")
    html_part.add_related(b"\x89PNG", "image", "png", cid="<b>")
    result = run(msg)
    assert result.removed_images == 3
    assert result.attachments == []
    assert "<img" not in result.html_body


def test_octet_stream_images_are_dropped_and_counted():
    result = run(mail(attachments=[("logo.png", "application", "octet-stream", b"\x89PNG"), pdf_att()]))
    assert isinstance(result, MaskedMail)
    assert result.removed_images == 1
    assert [name for name, _ in result.attachments] == ["CLIENT-A.pdf"]


def test_text_attachment_is_masked_and_kept():
    msg = mail(text="body", attachments=[("Zhang San notes.txt", "text", "plain", b"Account 12345678 of Zhang San")])
    result = run(msg)
    assert isinstance(result, MaskedMail)
    assert result.attachments == [("CLIENT-A notes.txt", b"Account ACCT-A01 of CLIENT-A")]
    assert result.hits == 4  # 标题 1 + 文件名 1 + 文本 2


def test_text_attachment_residual_is_held(monkeypatch):
    monkeypatch.setattr(masker, "_mask_txt", lambda data, rules: (data, 0))  # 模拟打码漏掉
    msg = mail(text="body", attachments=[("a.txt", "text", "plain", b"Zhang San")])
    assert run(msg) == Held("residual_keyword", "CLIENT-A statement")


def test_pdf_residual_is_held(monkeypatch):
    monkeypatch.setattr(engine, "mask_pdf_with_stats", lambda data, kw, max_pages=None: engine.MaskResult(data, 0))
    assert run(mail(attachments=[pdf_att("a.pdf")])).reason == "residual_keyword"


def encrypted_pdf():
    doc = fitz.open(stream=PDF, filetype="pdf")
    return doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")


@pytest.mark.parametrize("attachment,reason", [
    (pdf_att("Zhang San scan.pdf", scanned_pdf()), "scanned_pdf"),
    (pdf_att("Zhang San bad.pdf", b"not a pdf"), "invalid_pdf"),
    (pdf_att("Zhang San locked.pdf", encrypted_pdf()), "invalid_pdf"),
    (("Zhang San.xlsx", "application", "vnd.ms-excel", b"x"), "unsupported_attachment"),
    (("Zhang San.zip", "application", "zip", b"not a zip"), "bad_archive"),
])
def test_unmaskable_attachment_is_skipped_and_rest_kept(attachment, reason):
    result = run(mail(attachments=[attachment, pdf_att("ok.pdf")]))
    assert isinstance(result, MaskedMail)
    assert [name for name, _ in result.attachments] == ["ok.pdf"]
    [(name, why)] = result.skipped
    assert name.startswith("CLIENT-A") and why == reason  # 跳过的附件只记打码后的名字


def test_attached_email_is_skipped():
    msg = mail()
    msg.add_attachment(mail(subject="inner"))
    assert run(msg).skipped == [("attachment", "unsupported_attachment")]


def test_mail_without_skips_has_empty_skipped():
    assert run(mail(attachments=[pdf_att()])).skipped == []


def test_too_large_is_held():
    assert run(mail(attachments=[pdf_att()]), max_bytes=100).reason == "too_large"


def test_undecodable_mail_is_held():
    raw = b"Subject: x\r\nContent-Type: text/plain; charset=x-unknown-42\r\n\r\nZhang San\r\n"
    assert mask_mail(raw, KW) == Held("decode_error", "")


def test_style_text_is_not_escaped_after_rewrite():
    html = "<style>td>p{background:url(https://x/a.png)} .Zhang San{}</style><p>hi</p>"
    body = run(mail(text=None, html=html)).html_body
    assert "td>p{background:none}" in body and "&gt;" not in body
