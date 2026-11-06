"""邮件模式配置：从 TOML 读取运行参数、OAuth 客户端、租户与邮箱。打码规则不在这里（存 SQLite，见 rules.py）。"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PROVIDERS = ("imap", "gmail", "graph")
SINKS = ("fs", "smtp")
TENANT_CODE = re.compile(r"^[A-Za-z0-9-]+$")
DEFAULT_REDIRECT_URI = "http://localhost:18090/api/mail/oauth/callback"


class ConfigError(Exception):
    """配置不可用；message 只指出哪一项有问题，不含密码。"""


@dataclass(frozen=True)
class RunConfig:
    poll_seconds: int
    output_root: str
    retain_days: int
    timezone: str
    max_per_poll: int
    lookback_minutes: int
    originals_root: str = ""   # 有内容没能打码时整封原件存这里；空 = 不存


@dataclass(frozen=True)
class GoogleOAuth:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str


@dataclass(frozen=True)
class MicrosoftOAuth:
    client_id: str
    tenant: str


@dataclass(frozen=True)
class Mailbox:
    tenant: str
    address: str
    provider: str            # imap / gmail / graph
    sink: str                # fs / smtp
    password: str | None = field(repr=False)
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    smtp_to: str | None
    since: date | None       # 首次运行回溯起点，只在首次生效


@dataclass(frozen=True)
class Tenant:
    code: str
    mailboxes: tuple[Mailbox, ...]


@dataclass(frozen=True)
class MailConfig:
    run: RunConfig
    google: GoogleOAuth | None
    microsoft: MicrosoftOAuth | None
    tenants: tuple[Tenant, ...]

    def mailboxes(self) -> list[Mailbox]:
        return [m for t in self.tenants for m in t.mailboxes]

    def mailbox(self, address: str) -> Mailbox:
        for m in self.mailboxes():
            if m.address.lower() == address.lower():
                return m
        raise KeyError(address)

    def tenant(self, code: str) -> Tenant:
        for t in self.tenants:
            if t.code == code:
                return t
        raise KeyError(code)


def _text(data: dict, key: str, where: str, default: str | None = None) -> str:
    value = data.get(key)
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} is required")
    return value.strip()


def _optional_text(data: dict, key: str, where: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _int(data: dict, key: str, where: str, default: int, low: int, high: int) -> int:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where}.{key} must be an integer between {low} and {high}")
    return value


def _choice(data: dict, key: str, where: str, choices: tuple[str, ...], default: str | None = None) -> str:
    value = _text(data, key, where, default)
    if value not in choices:
        raise ConfigError(f"{where}.{key} must be one of {', '.join(choices)}")
    return value


def _output_root(value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or len(path.parts) < 3:  # ('/', 'srv', 'masked') 至少两层，拒绝 / 与顶层目录
        raise ConfigError("run.output_root must be an absolute path at least two levels deep, e.g. /srv/masked")
    return str(path)


def _originals_root(value: str, output_root: str) -> str:
    """原件不能放进落盘目录，否则下游 agent 能读到未打码的内容。"""
    if not value:
        return ""
    path, out = PurePosixPath(value), PurePosixPath(output_root)
    if not path.is_absolute() or len(path.parts) < 3:
        raise ConfigError("run.originals_root must be an absolute path at least two levels deep, e.g. /srv/originals")
    if path == out or out in path.parents or path in out.parents:
        raise ConfigError("run.originals_root must be outside run.output_root")
    return str(path)


def _run(data: dict) -> RunConfig:
    tz = _text(data, "timezone", "run", "Asia/Shanghai")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"run.timezone is not a known time zone: {tz}") from e
    output_root = _output_root(_text(data, "output_root", "run", "/srv/masked"))
    return RunConfig(
        poll_seconds=_int(data, "poll_seconds", "run", 60, 5, 3600),
        output_root=output_root,
        retain_days=_int(data, "retain_days", "run", 90, 1, 3650),
        timezone=tz,
        max_per_poll=_int(data, "max_per_poll", "run", 50, 1, 1000),
        lookback_minutes=_int(data, "lookback_minutes", "run", 10, 0, 1440),
        originals_root=_originals_root(_optional_text(data, "originals_root", "run") or "", output_root),
    )


def _since(data: dict, where: str) -> date | None:
    value = data.get("since")
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ConfigError(f"{where}.since must be a date like 2026-10-01")


def _mailbox(tenant: str, data: dict, where: str) -> Mailbox:
    address = _text(data, "address", where)
    provider = _choice(data, "provider", where, PROVIDERS)
    sink = _choice(data, "sink", where, SINKS, "fs")
    password = _optional_text(data, "password", where)
    if provider == "imap" and password is None:
        raise ConfigError(f"{where}.password is required for provider imap")
    smtp_to = _optional_text(data, "smtp_to", where)
    if sink == "smtp":
        if smtp_to is None:
            raise ConfigError(f"{where}.smtp_to is required when sink is smtp")
        if smtp_to.lower() == address.lower():
            raise ConfigError(f"{where}.smtp_to must differ from address")
        if password is None:
            raise ConfigError(f"{where}.password is required when sink is smtp")
    return Mailbox(
        tenant=tenant,
        address=address,
        provider=provider,
        sink=sink,
        password=password,
        imap_host=_text(data, "imap_host", where, "imap.larksuite.com"),
        imap_port=_int(data, "imap_port", where, 993, 1, 65535),
        smtp_host=_text(data, "smtp_host", where, "smtp.larksuite.com"),
        smtp_port=_int(data, "smtp_port", where, 465, 1, 65535),
        smtp_to=smtp_to,
        since=_since(data, where),
    )


def _tenants(data: dict) -> tuple[Tenant, ...]:
    raw = data.get("tenant")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("at least one [[tenant]] is required")
    tenants, codes, addresses = [], set(), set()
    for i, t in enumerate(raw):
        where = f"tenant[{i}]"
        code = _text(t, "code", where)
        if not TENANT_CODE.match(code):
            raise ConfigError(f"{where}.code: tenant code may only contain letters, digits and '-'")
        if code.lower() in codes:  # 落盘目录在 macOS 上不分大小写，LQ 与 lq 视为同一个
            raise ConfigError(f"Duplicate tenant code: {code}")
        codes.add(code.lower())
        raw_boxes = t.get("mailbox")
        if not isinstance(raw_boxes, list) or not raw_boxes:
            raise ConfigError(f"{where} needs at least one [[tenant.mailbox]]")
        boxes = []
        for j, m in enumerate(raw_boxes):
            box = _mailbox(code, m, f"{where}.mailbox[{j}]")
            key = box.address.lower()
            if key in addresses:
                raise ConfigError(f"Duplicate mailbox address: {box.address}")
            addresses.add(key)
            boxes.append(box)
        tenants.append(Tenant(code, tuple(boxes)))
    return tuple(tenants)


def _google(data: dict | None, needed: bool) -> GoogleOAuth | None:
    if data is None:
        if needed:
            raise ConfigError("[oauth.google] is required because a mailbox uses provider gmail")
        return None
    return GoogleOAuth(
        client_id=_text(data, "client_id", "oauth.google"),
        client_secret=_text(data, "client_secret", "oauth.google"),
        redirect_uri=_text(data, "redirect_uri", "oauth.google", DEFAULT_REDIRECT_URI),
    )


def _microsoft(data: dict | None, needed: bool) -> MicrosoftOAuth | None:
    if data is None:
        if needed:
            raise ConfigError("[oauth.microsoft] is required because a mailbox uses provider graph")
        return None
    return MicrosoftOAuth(client_id=_text(data, "client_id", "oauth.microsoft"), tenant=_text(data, "tenant", "oauth.microsoft"))


def load_config(path: str | Path) -> MailConfig:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Cannot read mail config {path}: {e}") from e
    tenants = _tenants(data)
    providers = {m.provider for t in tenants for m in t.mailboxes}
    oauth = data.get("oauth") or {}
    return MailConfig(
        run=_run(data.get("run") or {}),
        google=_google(oauth.get("google"), "gmail" in providers),
        microsoft=_microsoft(oauth.get("microsoft"), "graph" in providers),
        tenants=tenants,
    )
