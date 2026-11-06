"""命令行兜底（设计 §5.5）：`python -m file_masker.mail auth <邮箱>` / `auth --disconnect <邮箱>`。
与页面走同一套 OAuthFlows；设备码打印到终端；Google 在本机监听回调端口（配合 SSH 转发），端口被占则改为粘贴跳转地址。
读写同一个 state.db，服务可以同时在跑。"""
from __future__ import annotations

import argparse
import html
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

DEFAULT_PORT = 18090
POLL_S = 3
CALLBACK_PATH = "/api/mail/oauth/callback"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m file_masker.mail", description="file-masker mail mode tools")
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="authorize (or disconnect) an OAuth mailbox from the terminal")
    auth.add_argument("address", help="mailbox address as written in mail.toml")
    auth.add_argument("--disconnect", action="store_true", help="forget the stored authorization (local only)")
    auth.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the Google callback (default 18090)")
    auth.add_argument("--paste", action="store_true", help="do not listen for the Google callback; paste the redirected URL instead (use inside Docker)")
    auth.add_argument("--config", default=os.environ.get("MAIL_CONFIG"), help="mail.toml (default: $MAIL_CONFIG)")
    auth.add_argument("--data-dir", default=os.environ.get("MAIL_DATA_DIR", "/data"), help="state.db directory (default: $MAIL_DATA_DIR or /data)")
    return parser.parse_args(argv)


class CallbackServer:
    """只认 /api/mail/oauth/callback 的一次性本机监听。不打印请求行（含 code / state）。"""

    def __init__(self, flows, port: int) -> None:
        self.flows = flows
        self.result: str | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != CALLBACK_PATH:
                    self._reply(404, "Not found.")
                    return
                params = dict(urllib.parse.parse_qsl(parts.query))
                from file_masker.mail.oauth_flow import FlowError
                if params.get("error") or not params.get("code") or not params.get("state"):
                    outer.result = "error"
                    self._reply(400, f"Authorization was not completed ({html.escape(params.get('error') or 'missing code/state')}). You can close this tab.")
                    return
                try:
                    outer.flows.complete_callback(params["code"], params["state"])
                except FlowError:
                    outer.result = "error"
                    self._reply(400, "This authorization link is invalid or was already used. You can close this tab.")
                    return
                outer.result = "done"
                self._reply(200, "Authorization received. You can close this tab.")

            def _reply(self, status: int, message: str) -> None:
                body = f"<!doctype html><meta charset=utf-8><title>file-masker</title><p>{message}</p>".encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self._server.timeout = 1
        self.port = self._server.server_address[1]

    def serve_until_hit(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        try:
            while self.result is None and time.monotonic() < deadline:
                self._server.handle_request()
        finally:
            self._server.server_close()

    def close(self) -> None:
        self._server.server_close()


def run_auth(flows, address: str, disconnect: bool, port: int, sleep: Callable[[float], None] = time.sleep,
             server: CallbackServer | None = None, read_line: Callable[[str], str] = input, paste: bool = False) -> int:
    from file_masker.mail.oauth_flow import FlowError

    if disconnect:
        flows.disconnect(address)
        print(f"{address}: disconnected (local authorization removed; nothing revoked remotely).")
        return 0
    try:
        view = flows.start(address)
    except FlowError as e:
        print(f"{address}: {e}")
        return 1
    if view.mode == "device":
        print(f"Open {view.verification_uri} and enter the code: {view.user_code}")
        print(f"Sign in as {address}. Waiting (expires {view.expires_at.isoformat(timespec='minutes')})...")
        return _wait(flows, view.flow_id, address, sleep)
    print(f"Open this link in a browser and sign in as {address}:\n\n{view.auth_url}\n")
    if server is None and not paste:
        try:
            server = CallbackServer(flows, port)
        except OSError:
            print(f"Port {port} is busy (is the service running?).")
            paste = True
    if paste:
        print("After signing in, paste the full address of the page you land on below.")
        try:
            flows.complete_pasted(view.flow_id, read_line("Redirected URL: ").strip())
        except FlowError as e:
            print(f"{address}: {e}")
            return 1
        return _wait(flows, view.flow_id, address, sleep)
    print(f"Listening on 127.0.0.1:{server.port} for the callback (forward it from your machine with "
          f"`ssh -L {server.port}:127.0.0.1:{server.port} ...` if needed).")
    threading.Thread(target=lambda: server.serve_until_hit(timeout=600), daemon=True).start()
    return _wait(flows, view.flow_id, address, sleep)


def _wait(flows, flow_id: str, address: str, sleep: Callable[[float], None]) -> int:
    while True:
        status = flows.status(flow_id)
        if status["status"] == "done":
            print(f"{address}: authorized.")
            return 0
        if status["status"] == "error":
            print(f"{address}: failed: {status['message']}")
            return 1
        sleep(POLL_S)


def main(argv: list[str] | None = None) -> int:
    ns = parse_args(sys.argv[1:] if argv is None else argv)
    if not ns.config:
        print("mail.toml not given: set MAIL_CONFIG or pass --config.")
        return 2
    from file_masker.mail.config import ConfigError, load_config
    from file_masker.mail.httpclient import UrllibHttp
    from file_masker.mail.oauth2 import google_provider, microsoft_provider
    from file_masker.mail.oauth_flow import OAuthFlows
    from file_masker.mail.state import StateDb
    from file_masker.mail.worker import TokenManager

    try:
        config = load_config(ns.config)
    except ConfigError as e:
        print(str(e))
        return 2
    data_dir = Path(ns.data_dir)
    data_dir.mkdir(mode=0o700, exist_ok=True)
    state = StateDb(data_dir / "state.db")
    http = UrllibHttp()
    providers = {}
    if config.google:
        providers["google"] = google_provider(config.google.client_id, config.google.client_secret)
    if config.microsoft:
        providers["microsoft"] = microsoft_provider(config.microsoft.client_id, config.microsoft.tenant)
    tokens = TokenManager(state, providers, http)
    flows = OAuthFlows(state, providers, config, http, tokens)
    return run_auth(flows, ns.address, ns.disconnect, ns.port, paste=ns.paste)


if __name__ == "__main__":
    sys.exit(main())
