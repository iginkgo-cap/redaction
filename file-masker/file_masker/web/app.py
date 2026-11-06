"""离线打码 Web 服务：GET / 页面，POST /mask 单文件打码，GET /health 健康检查。
设置 MAIL_CONFIG 时另开邮件模式（LOOK-6764）：GET /mail 页面与 /api/mail 下的接口（设计 §6）。"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from file_masker.engine import MaskError, mask_pdf, min_keyword_length

if TYPE_CHECKING:
    from file_masker.mail.config import MailConfig
    from file_masker.mail.oauth_flow import OAuthFlows
    from file_masker.mail.rules import TenantRules
    from file_masker.mail.state import StateDb
    from file_masker.mail.worker import MailWorker, TokenManager

log = logging.getLogger("file_masker.web")

MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "100"))
MAX_KEYWORDS = 100
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
QUEUE_TIMEOUT_S = float(os.environ.get("QUEUE_TIMEOUT_S", "60"))
PROCESS_TIMEOUT_S = float(os.environ.get("PROCESS_TIMEOUT_S", "120"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "2000"))

try:
    VERSION = version("file-masker")
except PackageNotFoundError:
    VERSION = "dev"


@dataclass
class MailState:
    config: MailConfig
    state: StateDb
    rules: TenantRules
    tokens: TokenManager
    flows: OAuthFlows
    worker: MailWorker
    started_at: str
    lock: object = None   # /data/lock 的文件对象，持有到进程退出（E27）


mail_state: MailState | None = None  # 未设 MAIL_CONFIG 时为 None，邮件相关接口一律 404


def _start_mail() -> MailState | None:
    path = os.environ.get("MAIL_CONFIG")
    if not path:
        return None
    # 邮件模式依赖 mail 可选依赖组，只在启用时导入
    from file_masker.mail.config import load_config
    from file_masker.mail.httpclient import UrllibHttp
    from file_masker.mail.oauth2 import google_provider, microsoft_provider
    from file_masker.mail.oauth_flow import OAuthFlows
    from file_masker.mail.rules import TenantRules
    from file_masker.mail.sink import FsSink, SmtpSink
    from file_masker.mail.source_gmail import GmailSource
    from file_masker.mail.source_graph import GraphSource
    from file_masker.mail.source_imap import ImapSource
    from file_masker.mail.state import StateDb
    from file_masker.mail.worker import OAUTH_PROVIDER, MailWorker, TokenManager, acquire_lock

    mail_log = logging.getLogger("file_masker")
    if not mail_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        mail_log.addHandler(handler)
        mail_log.setLevel(logging.INFO)
    config = load_config(path)
    data_dir = Path(os.environ.get("MAIL_DATA_DIR", "/data"))
    data_dir.mkdir(mode=0o700, exist_ok=True)
    _require_private_dir(data_dir)
    lock = acquire_lock(data_dir / "lock")
    state = StateDb(data_dir / "state.db")
    rules = TenantRules(state)
    http = UrllibHttp()
    providers = {}
    if config.google:
        providers["google"] = google_provider(config.google.client_id, config.google.client_secret)
    if config.microsoft:
        providers["microsoft"] = microsoft_provider(config.microsoft.client_id, config.microsoft.tenant)
    tokens = TokenManager(state, providers, http)
    sources, sinks = {}, {}
    for box in config.mailboxes():
        if box.provider == "imap":
            sources[box.address] = ImapSource(box)
        else:
            cls = GmailSource if box.provider == "gmail" else GraphSource
            sources[box.address] = cls(
                box, lambda b=box: tokens.access_token(b.address, OAUTH_PROVIDER[b.provider]), http,
                lambda mail_id, b=box: state.is_processed(b.address, mail_id))
        sinks[box.address] = SmtpSink(box, VERSION) if box.sink == "smtp" else FsSink(config.run.output_root, config.run.timezone, VERSION)
    worker = MailWorker(config, state, rules, sources, sinks, tokens, VERSION)
    flows = OAuthFlows(state, providers, config, http, tokens)
    worker.start()
    log.info("mail mode on: %d tenants, %d mailboxes, output %s", len(config.tenants), len(config.mailboxes()), config.run.output_root)
    return MailState(config, state, rules, tokens, flows, worker, datetime.now().astimezone().isoformat(timespec="seconds"), lock)


def _require_private_dir(path: Path) -> None:
    """数据目录必须 700。bind mount 的挂载点上 chmod 可能被拒（colima），宿主机已是 700 时放过。"""
    import stat
    try:
        os.chmod(path, 0o700)
    except PermissionError:
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise
        log.warning("cannot chmod %s (mount point); mode is already 700, continuing", path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global mail_state
    mail_state = _start_mail()
    try:
        yield
    finally:
        if mail_state is not None:
            mail_state.worker.stop()
            if mail_state.lock is not None:
                mail_state.lock.close()
        mail_state = None


app = FastAPI(title="file-masker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
_slots = asyncio.Semaphore(MAX_CONCURRENT)
STATIC_DIR = Path(__file__).parent / "static"  # Vite 构建产物（frontend/ → npm run build）


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _parse_keywords(raw: str) -> dict[str, str] | str:
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError
        keywords = {}
        for item in items:
            source = str(item.get("source", "")).strip()
            if source:
                keywords[source] = str(item.get("target", ""))
    except (ValueError, AttributeError, TypeError):
        return "Invalid masking rules."
    if not keywords:
        return "Add at least one masking rule."
    if len(keywords) > MAX_KEYWORDS:
        return f"At most {MAX_KEYWORDS} masking rules."
    if any(len(k) < min_keyword_length(k) for k in keywords):
        return "Each text to mask needs at least 3 characters (2 for Chinese)."
    return keywords


def _masked_name(name: str | None) -> str:
    stem = Path(name or "file.pdf").stem
    return f"{stem}_masked.pdf"


@app.get("/")
def index() -> Response:
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return _error(503, "Frontend is not built. Run `npm run build` in frontend/.")
    # 每次回源校验：升级镜像后浏览器若沿用旧页面，会引用已不存在的旧 assets
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/mail")
def mail_page() -> Response:
    return index()  # 同一个前端，按路径切到邮件视图


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION}


# ---------- 邮件模式 ----------

MAIL_OFF = "Mail mode is not enabled."
CALLBACK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _rules_json(items) -> list[dict]:
    return [{"source": source, "target": target} for source, target in items]


def _mailbox_json(ms: MailState, box) -> dict:
    st = ms.worker.status(box.address)
    token = ms.state.get_token(box.address) if st.auth_state != "password" else None
    return {
        "address": box.address, "provider": box.provider, "sink": box.sink,
        "auth": {"state": st.auth_state, "account": token.account if token else None,
                 "grantedAt": token.granted_at if token else None},
        "lastPollAt": st.last_poll_at, "lastError": st.last_error, "backfill": st.backfill,
    }


def _record_json(r) -> dict:
    return {"at": r.at, "source": r.address, "subject": r.subject, "status": r.status, "reason": r.reason,
            "hits": r.hits, "attachments": r.attachments, "location": r.location, "skipped": r.skipped, "original": r.original}


def _callback_page(status: int, message: str) -> HTMLResponse:
    body = f"<!doctype html><meta charset=utf-8><title>file-masker</title><p>{message}</p>"
    return HTMLResponse(body, status_code=status, headers=CALLBACK_HEADERS)


@app.get("/api/mail")
def mail_status() -> Response:
    ms = mail_state
    if ms is None:
        return _error(404, MAIL_OFF)
    tenants = []
    for tenant in ms.config.tenants:
        tenants.append({
            "code": tenant.code,
            "mailboxes": [_mailbox_json(ms, box) for box in tenant.mailboxes],
            "rules": _rules_json(ms.rules.snapshot(tenant.code)),
            "records": [_record_json(r) for r in ms.state.recent(tenant.code)],
        })
    return JSONResponse({"pollSeconds": ms.config.run.poll_seconds, "startedAt": ms.started_at, "tenants": tenants})


@app.put("/api/mail/tenants/{code}/rules")
async def save_mail_rules(code: str, request: Request) -> Response:
    ms = mail_state
    if ms is None:
        return _error(404, MAIL_OFF)
    if not any(t.code == code for t in ms.config.tenants):
        return _error(404, "Unknown tenant.")
    try:
        items = await request.json()
    except ValueError:
        return _error(400, "Invalid rules.")
    from file_masker.mail.rules import RulesError
    try:
        saved = ms.rules.replace(code, items)
    except RulesError as e:
        return _error(400, str(e))
    ms.state.audit("rules_saved", code, _client(request))
    return JSONResponse({"rules": _rules_json(saved)})


@app.post("/api/mail/mailboxes/{address}/oauth")
def start_oauth(address: str, request: Request) -> Response:
    ms = mail_state
    if ms is None:
        return _error(404, MAIL_OFF)
    if not _known_mailbox(ms, address):
        return _error(404, "Unknown mailbox.")
    from file_masker.mail.oauth_flow import FlowError
    try:
        view = ms.flows.start(address)
    except FlowError as e:
        return _error(400, str(e))
    ms.state.audit("oauth_started", address, _client(request))
    return JSONResponse({"flowId": view.flow_id, "mode": view.mode, "authUrl": view.auth_url, "userCode": view.user_code,
                         "verificationUri": view.verification_uri, "expiresAt": view.expires_at.isoformat()})


@app.get("/api/mail/oauth/callback")   # 必须在 /api/mail/oauth/{flow_id} 之前注册
def oauth_callback(request: Request) -> Response:
    ms = mail_state
    if ms is None:
        return _error(404, MAIL_OFF)
    params = request.query_params
    if params.get("error"):
        return _callback_page(400, f"Authorization was not completed ({html.escape(params['error'])}). You can close this tab.")
    code, state = params.get("code"), params.get("state")
    if not code or not state:
        return _callback_page(400, "Missing code or state. You can close this tab.")
    from file_masker.mail.oauth_flow import FlowError
    try:
        ms.flows.complete_callback(code, state)
    except FlowError:
        return _callback_page(400, "This authorization link is invalid or was already used. You can close this tab.")
    return _callback_page(200, "Authorization received. You can close this tab and return to file-masker.")


@app.get("/api/mail/oauth/{flow_id}")
def oauth_status(flow_id: str) -> Response:
    ms = mail_state
    if ms is None:
        return _error(404, MAIL_OFF)
    return JSONResponse(ms.flows.status(flow_id))


@app.post("/api/mail/oauth/{flow_id}/complete")
async def oauth_complete(flow_id: str, request: Request) -> Response:
    ms = mail_state
    if ms is None:
        return _error(404, MAIL_OFF)
    try:
        body = await request.json()
        url = str(body.get("redirectedUrl") or "")
    except (ValueError, AttributeError):
        url = ""
    if not url:
        return _error(400, "Paste the full address of the page you landed on.")
    from file_masker.mail.oauth_flow import FlowError
    try:
        ms.flows.complete_pasted(flow_id, url)
    except FlowError as e:
        return _error(400, str(e))
    return JSONResponse(ms.flows.status(flow_id))


@app.delete("/api/mail/mailboxes/{address}/oauth", status_code=204)
def disconnect_oauth(address: str, request: Request) -> Response:
    ms = mail_state
    if ms is None:
        return _error(404, MAIL_OFF)
    if not _known_mailbox(ms, address):
        return _error(404, "Unknown mailbox.")
    ms.flows.disconnect(ms.config.mailbox(address).address)
    log.info("oauth disconnected for %s by %s", address, _client(request))
    return Response(status_code=204)


def _known_mailbox(ms: MailState, address: str) -> bool:
    try:
        ms.config.mailbox(address)
        return True
    except KeyError:
        return False


def _client(request: Request) -> str:
    return request.client.host if request.client else ""


@app.post("/mask")
async def mask(file: UploadFile = File(...), keywords: str = Form(...)) -> Response:
    parsed = _parse_keywords(keywords)
    if isinstance(parsed, str):
        return _error(400, parsed)
    data = await file.read(MAX_FILE_MB * 1024 * 1024 + 1)
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        return _error(413, f"Too large (max {MAX_FILE_MB} MB).")
    if not data.startswith(b"%PDF-"):
        return _error(400, "Not a valid PDF file.")
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=QUEUE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _error(503, "The service is busy. Please try again later.")
    started = time.monotonic()
    # 线程无法被强行终止：超时后它仍在后台运行，所以名额要等线程真正结束才归还
    task = asyncio.ensure_future(run_in_threadpool(mask_pdf, data, parsed, MAX_PAGES))
    task.add_done_callback(lambda _: _slots.release())
    try:
        out = await asyncio.wait_for(asyncio.shield(task), timeout=PROCESS_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _error(504, "Masking timed out.")
    except MaskError as e:
        return _error(422, str(e))
    except Exception:
        log.exception("mask failed")
        return _error(500, "Masking failed.")
    log.info("masked size=%d keywords=%d elapsed=%.2fs", len(data), len(parsed), time.monotonic() - started)
    return Response(content=out, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(_masked_name(file.filename))}"})


def _quote(name: str) -> str:
    from urllib.parse import quote
    return quote(name)


if (STATIC_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")
for _sub in ("cmaps", "standard_fonts"):
    if (STATIC_DIR / _sub).is_dir():
        app.mount(f"/{_sub}", StaticFiles(directory=STATIC_DIR / _sub), name=_sub)
