"""通用 OAuth2：授权码 + PKCE（Google）、设备码 RFC 8628（Microsoft）、刷新、身份核对。
两家只是端点与 scope 不同，协议部分共用；不引入 google-auth / msal（设计 D3）。日志不打印 token、授权码、state。"""
from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from file_masker.mail.httpclient import Http, HttpError

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
MICROSOFT_SCOPES = ["offline_access", "Mail.ReadWrite", "User.Read"]
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthError(Exception):
    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


class InvalidGrant(OAuthError):
    """refresh token / 授权码失效，重试无用：调用方把邮箱标为 expired。"""


@dataclass(frozen=True)
class Provider:
    name: str                 # google / microsoft
    auth_url: str
    token_url: str
    device_url: str | None
    scopes: list[str]
    client_id: str
    client_secret: str | None = field(repr=False)
    identity_url: str


@dataclass(frozen=True)
class Token:
    access_token: str = field(repr=False)
    expires_at: datetime
    refresh_token: str | None = field(repr=False)

    def expires_within(self, seconds: int) -> bool:
        return self.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=seconds)


@dataclass(frozen=True)
class DeviceCode:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    interval: int
    expires_at: datetime


def google_provider(client_id: str, client_secret: str) -> Provider:
    return Provider(
        name="google",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        device_url=None,
        scopes=GOOGLE_SCOPES,
        client_id=client_id,
        client_secret=client_secret,
        identity_url="https://gmail.googleapis.com/gmail/v1/users/me/profile",
    )


def microsoft_provider(client_id: str, tenant: str) -> Provider:
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return Provider(
        name="microsoft",
        auth_url=f"{base}/authorize",
        token_url=f"{base}/token",
        device_url=f"{base}/devicecode",
        scopes=MICROSOFT_SCOPES,
        client_id=client_id,
        client_secret=None,  # 公共客户端
        identity_url="https://graph.microsoft.com/v1.0/me",
    )


# ---- PKCE / state ----

def new_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()  # 43 字符


def code_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def new_state() -> str:
    return secrets.token_urlsafe(32)


# ---- 授权码（Google）----

def auth_code_url(provider: Provider, redirect_uri: str, login_hint: str, state: str, verifier: str) -> str:
    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "login_hint": login_hint,
    }
    if provider.name == "google":
        params.update(access_type="offline", prompt="consent")  # 确保拿到 refresh token
    return provider.auth_url + "?" + urllib.parse.urlencode(params)


def exchange_code(provider: Provider, http: Http, code: str, redirect_uri: str, verifier: str) -> Token:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": provider.client_id, "code_verifier": verifier}
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    return _token_request(provider, http, data)


# ---- 设备码（Microsoft）----

def start_device_code(provider: Provider, http: Http) -> DeviceCode:
    if not provider.device_url:
        raise OAuthError("unsupported", f"{provider.name} has no device code flow")
    body = _post(http, provider.device_url, {"client_id": provider.client_id, "scope": " ".join(provider.scopes)})
    return DeviceCode(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body.get("verification_uri") or body.get("verification_url"),
        interval=int(body.get("interval", 5)),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 900))),
    )


def poll_device_code(provider: Provider, http: Http, device_code: str) -> Token | str:
    """返回 Token，或 "pending" / "slow_down"；expired_token、access_denied 等终态抛 OAuthError。"""
    data = {"grant_type": DEVICE_GRANT, "device_code": device_code, "client_id": provider.client_id}
    try:
        return _token_request(provider, http, data)
    except OAuthError as e:
        if e.code in ("authorization_pending", "slow_down"):
            return "pending" if e.code == "authorization_pending" else "slow_down"
        raise


# ---- 刷新 / 身份 ----

def refresh(provider: Provider, http: Http, refresh_token: str) -> Token:
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": provider.client_id}
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    else:
        data["scope"] = " ".join(provider.scopes)  # Microsoft 公共客户端刷新时要重申 scope
    return _token_request(provider, http, data)


def identity(provider: Provider, http: Http, access_token: str) -> str:
    """返回授权账号的邮箱地址（小写），用于与配置里的地址核对。"""
    body = http.get_json(provider.identity_url, {"Authorization": f"Bearer {access_token}"})
    if provider.name == "google":
        address = body.get("emailAddress")
    else:
        address = body.get("mail") or body.get("userPrincipalName")
    if not address:
        raise OAuthError("no_identity", "identity endpoint returned no address")
    return str(address).strip().lower()


# ---- 内部 ----

def _post(http: Http, url: str, data: dict) -> dict:
    try:
        return http.post_form(url, data)
    except HttpError as e:
        body = e.body if isinstance(e.body, dict) else {}
        code = str(body.get("error") or f"http_{e.status}")
        if code == "invalid_grant":
            raise InvalidGrant(code, str(body.get("error_description", ""))) from None
        if 400 <= e.status < 500 and body.get("error"):   # 含 401：Microsoft 对 invalid_client 等返回 401
            raise OAuthError(code, str(body.get("error_description", ""))) from None
        raise  # 5xx / 无结构错误：交给调用方当临时故障


def _token_request(provider: Provider, http: Http, data: dict) -> Token:
    body = _post(http, provider.token_url, data)
    if "access_token" not in body:
        raise OAuthError(str(body.get("error", "no_access_token")), str(body.get("error_description", "")))
    return Token(
        access_token=body["access_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 3600))),
        refresh_token=body.get("refresh_token"),
    )
