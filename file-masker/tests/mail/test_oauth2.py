import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from file_masker.mail import oauth2
from file_masker.mail.httpclient import HttpError, Unauthorized
from file_masker.mail.oauth2 import (
    DeviceCode, InvalidGrant, OAuthError, Token, auth_code_url, exchange_code, google_provider, identity,
    microsoft_provider, new_state, new_verifier, poll_device_code, refresh, start_device_code,
)


class FakeHttp:
    """记录请求、按队列返回 (status, body)。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, kind, url, payload, headers):
        self.calls.append((kind, url, payload, headers))
        status, body = self.responses.pop(0)
        if status == 401:
            raise Unauthorized(url, body)
        if status >= 400:
            raise HttpError(status, body)
        return body

    def post_form(self, url, data, headers=None):
        return self._next("form", url, data, headers or {})

    def get_json(self, url, headers=None):
        return self._next("get", url, None, headers or {})


GOOGLE = google_provider("gid", "gsecret")
MS = microsoft_provider("mid", "look-tenant")


def test_pkce_verifier_and_challenge():
    v = new_verifier()
    assert len(v) == 43 and all(c.isalnum() or c in "-_" for c in v)
    assert new_verifier() != v
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert oauth2.code_challenge(v) == expected
    assert len(new_state()) >= 32


def test_google_auth_url():
    url = auth_code_url(GOOGLE, "http://localhost:18090/api/mail/oauth/callback", "lq@x.com", "st4te", "ver1fier")
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.netloc == "accounts.google.com"
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q["client_id"] == "gid" and q["redirect_uri"] == "http://localhost:18090/api/mail/oauth/callback"
    assert q["response_type"] == "code" and q["scope"] == "https://www.googleapis.com/auth/gmail.modify"
    assert q["access_type"] == "offline" and q["prompt"] == "consent" and q["login_hint"] == "lq@x.com"
    assert q["state"] == "st4te" and q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == oauth2.code_challenge("ver1fier")


def test_exchange_code_google():
    http = FakeHttp((200, {"access_token": "at", "expires_in": 3600, "refresh_token": "rt", "token_type": "Bearer"}))
    before = datetime.now(timezone.utc)
    tok = exchange_code(GOOGLE, http, "c0de", "http://localhost:18090/cb", "ver1fier")
    assert isinstance(tok, Token) and tok.access_token == "at" and tok.refresh_token == "rt"
    assert before + timedelta(seconds=3500) < tok.expires_at < before + timedelta(seconds=3700)
    kind, url, data, _ = http.calls[0]
    assert kind == "form" and url == "https://oauth2.googleapis.com/token"
    assert data == {"grant_type": "authorization_code", "code": "c0de", "redirect_uri": "http://localhost:18090/cb",
                    "client_id": "gid", "client_secret": "gsecret", "code_verifier": "ver1fier"}


def test_exchange_code_error_is_oauth_error_without_secret_leak():
    http = FakeHttp((400, {"error": "invalid_grant", "error_description": "Bad code"}))
    with pytest.raises(InvalidGrant) as exc:
        exchange_code(GOOGLE, http, "bad", "http://localhost/cb", "v")
    assert "gsecret" not in str(exc.value) and "invalid_grant" in str(exc.value)


def test_microsoft_device_code_flow():
    http = FakeHttp(
        (200, {"device_code": "dc", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin",
               "expires_in": 900, "interval": 5}),
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": "at", "expires_in": 3599, "refresh_token": "rt", "scope": "Mail.ReadWrite"}),
    )
    dc = start_device_code(MS, http)
    assert isinstance(dc, DeviceCode) and dc.user_code == "ABCD-EFGH" and dc.interval == 5
    assert dc.verification_uri == "https://microsoft.com/devicelogin"
    kind, url, data, _ = http.calls[0]
    assert url == "https://login.microsoftonline.com/look-tenant/oauth2/v2.0/devicecode"
    assert data == {"client_id": "mid", "scope": "offline_access Mail.ReadWrite User.Read"}

    assert poll_device_code(MS, http, "dc") == "pending"
    assert poll_device_code(MS, http, "dc") == "slow_down"
    tok = poll_device_code(MS, http, "dc")
    assert tok.access_token == "at" and tok.refresh_token == "rt"
    _, url, data, _ = http.calls[1]
    assert url == "https://login.microsoftonline.com/look-tenant/oauth2/v2.0/token"
    assert data == {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "dc", "client_id": "mid"}
    assert "code_verifier" not in data  # 设备码不用 PKCE


@pytest.mark.parametrize("error", ["expired_token", "access_denied", "bad_verification_code"])
def test_device_code_terminal_errors(error):
    http = FakeHttp((400, {"error": error, "error_description": "nope"}))
    with pytest.raises(OAuthError, match=error):
        poll_device_code(MS, http, "dc")


def test_device_code_401_with_aadsts_body_is_terminal_error():
    # Microsoft 对 invalid_client（如未开公共客户端流）返回 401 + 结构化错误：要当终态报出来，不能当 access token 失效无限退避
    http = FakeHttp((401, {"error": "invalid_client", "error_description": "AADSTS7000218: ... client_assertion or client_secret"}))
    with pytest.raises(OAuthError) as e:
        poll_device_code(MS, http, "dc")
    assert e.value.code == "invalid_client" and "AADSTS7000218" in e.value.description


def test_refresh_keeps_old_refresh_token_when_absent():
    http = FakeHttp((200, {"access_token": "at2", "expires_in": 3600}))
    tok = refresh(GOOGLE, http, "rt-old")
    assert tok.access_token == "at2" and tok.refresh_token is None
    _, url, data, _ = http.calls[0]
    assert data == {"grant_type": "refresh_token", "refresh_token": "rt-old", "client_id": "gid", "client_secret": "gsecret"}


def test_refresh_microsoft_rotates_and_needs_no_secret():
    http = FakeHttp((200, {"access_token": "at2", "expires_in": 3600, "refresh_token": "rt-new"}))
    tok = refresh(MS, http, "rt-old")
    assert tok.refresh_token == "rt-new"
    _, _, data, _ = http.calls[0]
    assert data == {"grant_type": "refresh_token", "refresh_token": "rt-old", "client_id": "mid",
                    "scope": "offline_access Mail.ReadWrite User.Read"}


def test_refresh_invalid_grant_vs_transient():
    with pytest.raises(InvalidGrant):
        refresh(GOOGLE, FakeHttp((400, {"error": "invalid_grant"})), "rt")
    with pytest.raises(HttpError):  # 5xx 不是授权失效，调用方保持原状下一轮再试
        refresh(GOOGLE, FakeHttp((503, {"error": "backend"})), "rt")


def test_identity_google_and_microsoft():
    http = FakeHttp((200, {"emailAddress": "LQ-Ops@example.com"}))
    assert identity(GOOGLE, http, "at") == "lq-ops@example.com"
    assert http.calls[0][1] == "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    assert http.calls[0][3]["Authorization"] == "Bearer at"
    http = FakeHttp((200, {"mail": None, "userPrincipalName": "Ops@look.example"}))
    assert identity(MS, http, "at") == "ops@look.example"
    assert http.calls[0][1] == "https://graph.microsoft.com/v1.0/me"


def test_identity_unauthorized_propagates():
    with pytest.raises(Unauthorized):
        identity(GOOGLE, FakeHttp((401, {})), "bad")


def test_token_expiry_margin():
    tok = Token("at", datetime.now(timezone.utc) + timedelta(seconds=30), None)
    assert tok.expires_within(60) and not tok.expires_within(10)
