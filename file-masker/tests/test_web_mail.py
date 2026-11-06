import logging
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from file_masker.mail.config import GoogleOAuth, Mailbox, MailConfig, MicrosoftOAuth, RunConfig, Tenant
from file_masker.mail.httpclient import HttpError
from file_masker.mail.oauth2 import google_provider, microsoft_provider
from file_masker.mail.oauth_flow import OAuthFlows
from file_masker.mail.rules import TenantRules
from file_masker.mail.state import ProcessedRow, StateDb
from file_masker.mail.worker import MailWorker, TokenManager
from file_masker.web import app as web

REDIRECT = "http://localhost:18090/api/mail/oauth/callback"
GMAIL = Mailbox("lq", "lq-ops@example.com", "gmail", "fs", None, None, 993, None, 465, None, None)
IMAP = Mailbox("demo", "src@example.com", "imap", "smtp", "s3cret-pw", "imap.secret-host", 993, "smtp.secret-host", 465,
               "dst@example.com", None)
CONFIG = MailConfig(RunConfig(30, "/srv/masked", 90, "Asia/Shanghai", 50, 10), GoogleOAuth("gcid", "g-secret", REDIRECT),
                    MicrosoftOAuth("mcid", "tenant-1"), (Tenant("lq", (GMAIL,)), Tenant("demo", (IMAP,))))
PROVIDERS = {"google": google_provider("gcid", "g-secret"), "microsoft": microsoft_provider("mcid", "tenant-1")}
TOKEN = {"access_token": "at-secret", "expires_in": 3600, "refresh_token": "rt-secret"}
client = TestClient(web.app)


class FakeHttp:
    def __init__(self):
        self.post_responses, self.get_responses = [], []

    def post_form(self, url, data, headers=None):
        status, body = self.post_responses.pop(0)
        if status >= 400:
            raise HttpError(status, body)
        return body

    def get_json(self, url, headers=None):
        return self.get_responses.pop(0)


class FakeSource:
    def open(self): pass
    def close(self): pass
    def initial_cursor(self, since): return {}
    def cursor_time(self, cursor): return None
    def scan(self, cursor, window): raise AssertionError("not polled in web tests")
    def fetch(self, mail_id): raise AssertionError
    def mark_done(self, mail_id): pass


@pytest.fixture
def mail_on(tmp_path, monkeypatch):
    state = StateDb(tmp_path / "state.db")
    rules = TenantRules(state)
    http = FakeHttp()
    tokens = TokenManager(state, PROVIDERS, http)
    flows = OAuthFlows(state, PROVIDERS, CONFIG, http, tokens, spawn=lambda fn: None)
    sources = {b.address: FakeSource() for b in CONFIG.mailboxes()}
    worker = MailWorker(CONFIG, state, rules, sources, {}, tokens, "1.3.0")
    ms = web.MailState(CONFIG, state, rules, tokens, flows, worker, "2026-10-20T10:00:00+08:00")
    monkeypatch.setattr(web, "mail_state", ms)
    ms.http = http
    return ms


def state_of(flow_id):
    return client.get(f"/api/mail/oauth/{flow_id}").json()


def own_logs(caplog):
    """只看服务自己的日志（httpx 测试客户端会把请求 URL 打出来，那不是服务的行为）。"""
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("file_masker"))


# ---------- 开关 ----------

def test_all_mail_endpoints_404_when_disabled(monkeypatch):
    monkeypatch.setattr(web, "mail_state", None)
    assert client.get("/api/mail").status_code == 404
    assert client.put("/api/mail/tenants/lq/rules", json=[]).status_code == 404
    assert client.post("/api/mail/mailboxes/a@b.c/oauth").status_code == 404
    assert client.get("/api/mail/oauth/x").status_code == 404
    assert client.get("/api/mail/oauth/callback?code=1&state=2").status_code == 404
    assert client.post("/api/mail/oauth/x/complete", json={"redirectedUrl": "u"}).status_code == 404
    assert client.delete("/api/mail/mailboxes/a@b.c/oauth").status_code == 404


def test_mail_page_serves_frontend(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<title>PDF Masking</title>")
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path)
    resp = client.get("/mail")
    assert "PDF Masking" in resp.text and resp.headers["cache-control"] == "no-cache"


# ---------- GET /api/mail ----------

def test_status_aggregates_tenants_without_secrets(mail_on):
    st = mail_on.state
    st.save_token("lq-ops@example.com", "google", "lq-ops@example.com", "rt-secret")
    st.init_mailbox_state("lq-ops@example.com", {"ts": 1}, "2026-10-16T00:00:00+00:00")
    st.set_scan_until("lq-ops@example.com", "2026-10-14T00:00:00+00:00")
    st.record_processed_and_advance(ProcessedRow("lq-ops@example.com", "m1", "lq", "written", "", "CLIENT-A stmt", 7, 1,
                                                 "/srv/masked/lq/x", "2026-10-16T09:00:00+08:00"), {"ts": 2})
    mail_on.rules.replace("lq", [{"source": "Zhang San", "target": "CLIENT-A"}])
    resp = client.get("/api/mail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pollSeconds"] == 30 and data["startedAt"] == "2026-10-20T10:00:00+08:00"
    lq, demo = data["tenants"]
    assert lq["code"] == "lq" and lq["rules"] == [{"source": "Zhang San", "target": "CLIENT-A"}]
    [box] = lq["mailboxes"]
    assert box["address"] == "lq-ops@example.com" and box["provider"] == "gmail" and box["sink"] == "fs"
    assert box["auth"]["state"] == "authorized" and box["auth"]["account"] == "lq-ops@example.com" and box["auth"]["grantedAt"]
    assert box["lastPollAt"] is None and box["lastError"] is None and box["backfill"] == "2026-10-14T00:00:00+00:00"
    assert lq["records"] == [{"at": "2026-10-16T09:00:00+08:00", "source": "lq-ops@example.com", "subject": "CLIENT-A stmt",
                              "status": "written", "reason": "", "hits": 7, "attachments": 1, "location": "/srv/masked/lq/x",
                              "skipped": 0, "original": ""}]
    [dbox] = demo["mailboxes"]
    assert dbox["auth"] == {"state": "password", "account": None, "grantedAt": None} and dbox["sink"] == "smtp"
    assert demo["rules"] == [] and demo["records"] == []
    for secret in ("s3cret", "secret-host", "rt-secret", "g-secret", "tenant-1", "gcid"):
        assert secret not in resp.text


# ---------- 规则 ----------

def test_save_rules_per_tenant_with_audit(mail_on):
    rules = [{"source": "Zhang San", "target": "CLIENT-A"}, {"source": "张三", "target": ""}]
    resp = client.put("/api/mail/tenants/lq/rules", json=rules)
    assert resp.status_code == 200 and resp.json() == {"rules": rules}
    assert mail_on.rules.snapshot("lq") == (("Zhang San", "CLIENT-A"), ("张三", "")) and mail_on.rules.snapshot("demo") == ()
    [audit] = mail_on.state.recent_audit()
    assert (audit.action, audit.target) == ("rules_saved", "lq") and audit.client
    assert client.put("/api/mail/tenants/nope/rules", json=rules).status_code == 404


@pytest.mark.parametrize("body,msg", [
    ([{"source": "Li", "target": "X"}], "at least 3"),
    ({"source": "Zhang San"}, "list"),
    ("not json", "Invalid"),
])
def test_invalid_rules_rejected(mail_on, body, msg):
    mail_on.rules.replace("lq", [{"source": "Zhang San", "target": "CLIENT-A"}])
    if body == "not json":
        resp = client.put("/api/mail/tenants/lq/rules", content=b"{oops", headers={"content-type": "application/json"})
    else:
        resp = client.put("/api/mail/tenants/lq/rules", json=body)
    assert resp.status_code == 400 and msg in resp.json()["error"]
    assert mail_on.rules.snapshot("lq") == (("Zhang San", "CLIENT-A"),) and mail_on.state.recent_audit() == []


# ---------- OAuth ----------

def test_google_authorize_via_callback(mail_on, caplog):
    caplog.set_level(logging.DEBUG)
    resp = client.post("/api/mail/mailboxes/lq-ops@example.com/oauth")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "code" and body["flowId"] and body["expiresAt"] and body["userCode"] is None
    st = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(body["authUrl"]).query))["state"]
    assert client.post("/api/mail/mailboxes/lq-ops@example.com/oauth").json()["flowId"] == body["flowId"]  # E26
    assert state_of(body["flowId"]) == {"status": "pending", "message": ""}
    mail_on.http.post_responses = [(200, TOKEN)]
    mail_on.http.get_responses = [{"emailAddress": "lq-ops@example.com"}]
    cb = client.get("/api/mail/oauth/callback", params={"code": "4/secret-code", "state": st})
    assert cb.status_code == 200 and "close this tab" in cb.text and cb.headers["content-type"].startswith("text/html")
    assert cb.headers["cache-control"] == "no-store" and cb.headers["referrer-policy"] == "no-referrer"
    assert state_of(body["flowId"]) == {"status": "done", "message": ""}
    assert mail_on.state.get_token("lq-ops@example.com").refresh_token == "rt-secret"
    assert client.get("/api/mail").json()["tenants"][0]["mailboxes"][0]["auth"]["state"] == "authorized"
    assert "oauth" in own_logs(caplog)
    for secret in ("secret-code", st, "rt-secret", "at-secret"):
        assert secret not in own_logs(caplog)


def test_callback_rejects_forged_state_and_denied(mail_on, caplog):
    caplog.set_level(logging.DEBUG)
    client.post("/api/mail/mailboxes/lq-ops@example.com/oauth")
    bad = client.get("/api/mail/oauth/callback", params={"code": "c", "state": "forged-state-value"})
    assert bad.status_code == 400 and "forged-state-value" not in own_logs(caplog)
    denied = client.get("/api/mail/oauth/callback", params={"error": "access_denied"})
    assert denied.status_code == 400 and "access_denied" in denied.text
    xss = client.get("/api/mail/oauth/callback", params={"error": "<script>alert(1)</script>"})
    assert xss.status_code == 400 and "<script>" not in xss.text and "&lt;script&gt;" in xss.text
    assert client.get("/api/mail/oauth/callback").status_code == 400


def test_pasted_completion(mail_on):
    flow_id = client.post("/api/mail/mailboxes/lq-ops@example.com/oauth").json()["flowId"]
    assert client.post(f"/api/mail/oauth/{flow_id}/complete", json={"redirectedUrl": "http://x/?nothing=1"}).status_code == 400
    assert client.post(f"/api/mail/oauth/{flow_id}/complete", json={}).status_code == 400
    assert client.post("/api/mail/oauth/nope/complete", json={"redirectedUrl": "http://x/?code=1&state=2"}).status_code == 400


def test_authorize_rejects_imap_or_unknown_mailbox(mail_on):
    assert client.post("/api/mail/mailboxes/src@example.com/oauth").status_code == 400
    assert client.post("/api/mail/mailboxes/nobody@example.com/oauth").status_code == 404


def test_unknown_flow_status(mail_on):
    assert state_of("nope") == {"status": "error", "message": "unknown flow"}


def test_disconnect(mail_on):
    mail_on.state.save_token("lq-ops@example.com", "google", "lq-ops@example.com", "rt-secret")
    assert client.delete("/api/mail/mailboxes/lq-ops@example.com/oauth").status_code == 204
    assert mail_on.state.get_token("lq-ops@example.com") is None
    assert [a.action for a in mail_on.state.recent_audit()] == ["oauth_disconnected"]
    assert client.delete("/api/mail/mailboxes/nobody@example.com/oauth").status_code == 404


# ---------- lifespan ----------

def write_config(tmp_path):
    example = (Path(__file__).parents[1] / "mail.example.toml").read_text(encoding="utf-8")
    config = tmp_path / "mail.toml"
    config.write_text(example.replace('client_secret = ""', 'client_secret = "x"'), encoding="utf-8")
    return config


def test_lifespan_wires_everything(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(MailWorker, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(MailWorker, "stop", lambda self: calls.append("stop"))
    monkeypatch.setenv("MAIL_CONFIG", str(write_config(tmp_path)))
    monkeypatch.setenv("MAIL_DATA_DIR", str(tmp_path / "data"))
    with TestClient(web.app) as c:
        data = c.get("/api/mail").json()
        assert [t["code"] for t in data["tenants"]] == ["lq", "look", "demo"] and data["startedAt"]
        assert data["tenants"][0]["mailboxes"][0]["auth"]["state"] == "not_authorized"
        assert (tmp_path / "data" / "lock").exists() and oct((tmp_path / "data" / "state.db").stat().st_mode)[-3:] == "600"
        assert oct((tmp_path / "data").stat().st_mode)[-3:] == "700"
    assert calls == ["start", "stop"] and web.mail_state is None


def test_lifespan_fails_on_bad_config_or_held_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAIL_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(Exception):
        with TestClient(web.app):
            pass
    monkeypatch.setenv("MAIL_CONFIG", str(write_config(tmp_path)))
    monkeypatch.setattr(MailWorker, "start", lambda self: None)
    monkeypatch.setattr(MailWorker, "stop", lambda self: None)
    from file_masker.mail.worker import acquire_lock
    held = acquire_lock(Path(tmp_path / "data" / "lock")) if (tmp_path / "data").exists() else None
    if held is None:
        (tmp_path / "data").mkdir()
        held = acquire_lock(tmp_path / "data" / "lock")
    with pytest.raises(Exception):
        with TestClient(web.app):
            pass
    held.close()
    assert web.mail_state is None


def test_data_dir_chmod_refused_is_tolerated_only_when_already_private(tmp_path, monkeypatch):
    d = tmp_path / "d"; d.mkdir(mode=0o700)
    def refuse(*a, **k): raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(web.os, "chmod", refuse)
    web._require_private_dir(d)                      # 已是 700：放过
    loose = tmp_path / "loose"; loose.mkdir(mode=0o755)
    with pytest.raises(PermissionError):
        web._require_private_dir(loose)              # 不是 700 又改不了：拒绝启动


def test_lifespan_without_config_keeps_mail_off(monkeypatch):
    monkeypatch.delenv("MAIL_CONFIG", raising=False)
    with TestClient(web.app) as c:
        assert c.get("/api/mail").status_code == 404
        assert c.get("/health").status_code == 200
