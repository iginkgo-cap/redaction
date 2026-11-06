"""页面 / 命令行发起的授权流程（设计 §5.4）。流程只在内存，10 分钟过期，每邮箱同一时间一个（E26）。
状态机 pending → exchanging → done | error，锁内 CAS；`state`/`verifier` 单次消费（E5）。
完成前核对账号（E2）与 auth_generation（E32）。Disconnect 只动本地（D20）。日志不打印 code、state、token。"""
from __future__ import annotations

import logging
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from file_masker.mail.config import MailConfig
from file_masker.mail.httpclient import Http, HttpError
from file_masker.mail.oauth2 import (DeviceCode, OAuthError, Provider, Token, auth_code_url, exchange_code, identity,
                                     new_state, new_verifier, poll_device_code, start_device_code)
from file_masker.mail.state import StateDb
from file_masker.mail.worker import OAUTH_PROVIDER, TokenManager

log = logging.getLogger("file_masker.mail.oauth")

FLOW_TTL = timedelta(minutes=10)
SLOW_DOWN_S = 5


class FlowError(Exception):
    """调用方错误（400）：邮箱不支持 OAuth、state 不匹配、流程不存在……"""


@dataclass(frozen=True)
class FlowView:
    flow_id: str
    mode: str                       # code / device
    expires_at: datetime
    auth_url: str | None = None
    user_code: str | None = None
    verification_uri: str | None = None


class _Flow:
    def __init__(self, address: str, provider_name: str, mode: str, generation: int, expires_at: datetime) -> None:
        self.flow_id = secrets.token_urlsafe(16)
        self.address, self.provider_name, self.mode = address, provider_name, mode
        self.generation, self.expires_at = generation, expires_at
        self.status, self.message = "pending", ""
        self.state: str | None = None
        self.verifier: str | None = None
        self.auth_url: str | None = None
        self.device: DeviceCode | None = None


class OAuthFlows:
    def __init__(self, state: StateDb, providers: dict[str, Provider], config: MailConfig, http: Http,
                 tokens: TokenManager, clock: Callable[[], datetime] | None = None,
                 spawn: Callable[[Callable[[], None]], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._state = state
        self._providers = providers
        self._config = config
        self._http = http
        self._tokens = tokens
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, name="oauth-device-poll", daemon=True).start())
        self._sleep = sleep
        self._lock = threading.Lock()
        self._flows: dict[str, _Flow] = {}

    # ---- 发起 ----

    def start(self, address: str) -> FlowView:
        try:
            box = self._config.mailbox(address)
        except KeyError:
            raise FlowError(f"unknown mailbox: {address}") from None
        provider_name = OAUTH_PROVIDER.get(box.provider)
        if provider_name is None:
            raise FlowError(f"{address} uses password login; nothing to authorize")
        provider = self._providers[provider_name]
        with self._lock:
            existing = self._for_address(box.address)
            if existing and existing.status in ("pending", "exchanging") and not self._expired(existing):
                return self._view(existing)
        now = self._clock()
        generation = self._state.get_generation(box.address)
        if provider.device_url:
            flow = self._start_device(box.address, provider_name, provider, generation, now)
        else:
            flow = _Flow(box.address, provider_name, "code", generation, now + FLOW_TTL)
            flow.state, flow.verifier = new_state(), new_verifier()
            flow.auth_url = auth_code_url(provider, self._config.google.redirect_uri, box.address, flow.state, flow.verifier)
        with self._lock:
            for old in list(self._flows.values()):
                if old.address == box.address:
                    self._finish(old, "error", "superseded")  # 并发发起时后来者为准，旧流程的后台轮询据此退出
                    del self._flows[old.flow_id]
            self._flows[flow.flow_id] = flow
        if flow.mode == "device":
            self._spawn(lambda: self._poll_device(flow))
        log.info("oauth flow started for %s (%s)", box.address, flow.mode)
        return self._view(flow)

    def _start_device(self, address: str, provider_name: str, provider: Provider, generation: int, now: datetime) -> _Flow:
        try:
            device = start_device_code(provider, self._http)
        except (OAuthError, HttpError, OSError) as e:
            raise FlowError(f"device code request failed: {e}") from e
        ttl = min(FLOW_TTL, device.expires_at - datetime.now(timezone.utc))
        flow = _Flow(address, provider_name, "device", generation, now + ttl)
        flow.device = device
        return flow

    # ---- 查询 ----

    def status(self, flow_id: str) -> dict:
        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is None:
                return {"status": "error", "message": "unknown flow"}
            if flow.status in ("pending", "exchanging") and self._expired(flow):
                self._finish(flow, "error", "expired")
            return {"status": flow.status, "message": flow.message}

    # ---- 完成（Google）----

    def complete_callback(self, code: str, state: str) -> None:
        with self._lock:
            wanted = state.encode("utf-8", "surrogateescape")  # compare_digest 不接受非 ASCII 的 str
            flow = next((f for f in self._flows.values() if f.state and secrets.compare_digest(f.state.encode(), wanted)), None)
            if flow is None or flow.status != "pending" or self._expired(flow):
                raise FlowError("unknown or already used state")
            flow.status, flow.state = "exchanging", None   # CAS + state 单次消费
            verifier, flow.verifier = flow.verifier, None
        provider = self._providers[flow.provider_name]
        try:
            token = exchange_code(provider, self._http, code, self._config.google.redirect_uri, verifier)
        except (OAuthError, HttpError, OSError) as e:
            self._finish(flow, "error", str(e))
            return
        self._complete(flow, token)

    def complete_pasted(self, flow_id: str, redirected_url: str) -> None:
        with self._lock:
            if flow_id not in self._flows:
                raise FlowError("unknown flow")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(redirected_url.strip()).query))
        if not params.get("code") or not params.get("state"):
            raise FlowError("the pasted address has no code/state; copy the full address of the page you landed on")
        self.complete_callback(params["code"], params["state"])

    # ---- 设备码轮询（后台）----

    def _poll_device(self, flow: _Flow) -> None:
        provider = self._providers[flow.provider_name]
        interval = flow.device.interval
        while True:
            self._sleep(interval)
            with self._lock:
                if flow.status != "pending":
                    return
                if self._expired(flow):
                    self._finish(flow, "error", "expired")
                    return
            try:
                outcome = poll_device_code(provider, self._http, flow.device.device_code)
            except OAuthError as e:
                self._finish(flow, "error", str(e))
                return
            except (HttpError, OSError) as e:   # 超时 / 5xx：退避后继续
                log.warning("device code poll for %s failed: %s", flow.address, e)
                interval += SLOW_DOWN_S
                continue
            if outcome == "slow_down":
                interval += SLOW_DOWN_S
                continue
            if outcome == "pending":
                continue
            with self._lock:
                if flow.status != "pending":
                    return
                flow.status = "exchanging"
            self._complete(flow, outcome)
            return

    # ---- 公共收尾 ----

    def _complete(self, flow: _Flow, token: Token) -> None:
        provider = self._providers[flow.provider_name]
        if not token.refresh_token:
            self._finish(flow, "error", "no refresh token in the response; remove the app's access in the account and try again")
            return
        try:
            account = identity(provider, self._http, token.access_token)
        except (OAuthError, HttpError, OSError) as e:
            self._finish(flow, "error", f"identity check failed: {e}")
            return
        if account != flow.address.lower():
            self._finish(flow, "error", f"authorized {account}, but this mailbox is {flow.address}")
            return
        with self._lock:
            if flow.status != "exchanging" or self._state.get_generation(flow.address) != flow.generation:
                self._finish(flow, "error", "cancelled")   # 期间被 Disconnect（E32）
                return
            self._state.save_token(flow.address, flow.provider_name, account, token.refresh_token)
            self._tokens.forget(flow.address)
            self._state.audit("oauth_authorized", flow.address)
            self._finish(flow, "done", "")
        log.info("oauth authorized for %s", flow.address)

    # ---- Disconnect ----

    def disconnect(self, address: str) -> None:
        with self._lock:
            self._state.bump_generation(address)
            for flow in self._flows.values():
                if flow.address == address and flow.status in ("pending", "exchanging"):
                    self._finish(flow, "error", "cancelled")
            self._tokens.forget(address)
            self._state.delete_token(address)
            self._state.audit("oauth_disconnected", address)
        log.info("oauth disconnected for %s (local only)", address)

    # ---- 内部 ----

    def _for_address(self, address: str) -> _Flow | None:
        return next((f for f in self._flows.values() if f.address == address), None)

    def _expired(self, flow: _Flow) -> bool:
        return self._clock() >= flow.expires_at

    @staticmethod
    def _finish(flow: _Flow, status: str, message: str) -> None:
        if flow.status in ("done", "error"):
            return
        flow.status, flow.message = status, message
        flow.state = flow.verifier = None

    @staticmethod
    def _view(flow: _Flow) -> FlowView:
        return FlowView(flow.flow_id, flow.mode, flow.expires_at, flow.auth_url,
                        flow.device.user_code if flow.device else None,
                        flow.device.verification_uri if flow.device else None)
