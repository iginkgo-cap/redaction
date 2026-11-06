import smtplib
from datetime import datetime, timezone

import pytest

from file_masker.mail import sink as sink_mod
from file_masker.mail.config import Mailbox
from file_masker.mail.mime import MaskedMail
from file_masker.mail.sink import MailContext, PermanentSendError, SmtpSink

MAILBOX = Mailbox(tenant="demo", address="src@example.com", provider="imap", sink="smtp", password="pw",
                  imap_host="imap.x", imap_port=993, smtp_host="smtp.x", smtp_port=465,
                  smtp_to="dst@example.com", since=None)
MAIL = MaskedMail("CLIENT-A statement", "RM <rm@bank>", "Tue, 13 Oct 2026 10:00:00 +0800", "plain", None,
                  [("a.pdf", b"%PDF-1.4 x")], 0, 3)
CTX = MailContext("demo", "src@example.com", "imap", "7-41", datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc), "sha256:x")


def fake_smtp(monkeypatch, calls, error=None):
    class FakeSmtp:
        def __init__(self, host, port, timeout=None):
            calls.append(("CONNECT", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("QUIT",))

        def login(self, user, password):
            calls.append(("LOGIN", user, password))

        def send_message(self, msg):
            if error:
                raise error
            calls.append(("SEND", msg["From"], msg["To"], msg["Subject"], msg["X-File-Masker"]))

    monkeypatch.setattr(sink_mod.smtplib, "SMTP_SSL", FakeSmtp)


def test_sends_new_mail_from_source_to_target(monkeypatch):
    calls = []
    fake_smtp(monkeypatch, calls)
    out = SmtpSink(MAILBOX, "1.3.0").deliver(MAIL, CTX)
    assert calls == [("CONNECT", "smtp.x", 465), ("LOGIN", "src@example.com", "pw"),
                     ("SEND", "src@example.com", "dst@example.com", "CLIENT-A statement", "1.3.0"), ("QUIT",)]
    assert (out.location, out.subject, out.hits, out.attachments, out.reused) == ("dst@example.com", "CLIENT-A statement", 3, 1, False)


@pytest.mark.parametrize("error", [
    smtplib.SMTPDataError(552, b"message size exceeds limit"),
    smtplib.SMTPSenderRefused(550, b"sender not allowed", "src@example.com"),
    smtplib.SMTPRecipientsRefused({"dst@example.com": (550, b"no such user")}),
])
def test_permanent_rejection(monkeypatch, error):
    fake_smtp(monkeypatch, [], error)
    with pytest.raises(PermanentSendError):
        SmtpSink(MAILBOX, "1.3.0").deliver(MAIL, CTX)


def test_temporary_error_propagates_as_is(monkeypatch):
    fake_smtp(monkeypatch, [], smtplib.SMTPDataError(451, b"try again later"))
    with pytest.raises(smtplib.SMTPDataError):
        SmtpSink(MAILBOX, "1.3.0").deliver(MAIL, CTX)
