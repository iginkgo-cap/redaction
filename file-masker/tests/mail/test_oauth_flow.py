import urllib.parse
from datetime import date, datetime, timedelta, timezone

import pytest

from file_masker.mail.config import GoogleOAuth, Mailbox, MailConfig, MicrosoftOAuth, RunConfig, Tenant
from file_masker.mail.httpclient import HttpError
from file_masker.mail.oauth2 import DEVICE_GRANT as DEVICE_GRANT_TYPE, google_provider, microsoft_provider
from file_masker.mail.oauth_flow import FLOW_TTL, FlowError, OAuthFlows
from file_masker.mail.state import StateDb
from file_masker.mail.worker import TokenManager

T0 = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
REDIRECT = "http://localhost:18090/api/mail/oauth/callback"
GMAIL = Mailbox("lq", "lq-ops@example.com", "gmail", "fs", None, None, 993, None, 465, None, None)
GRAPH = Mailbox("look", "ops@look.example", "graph", "fs", None, None, 993, None, 465, None, None)
IMAP = Mailbox("demo", "demo@example.com", "imap", "smtp", "pw", "imap.x", 993, "smtp.x", 465, "dst@example.com", None)
CONFIG = MailConfig(RunConfig(60, "/srv/masked", 90, "UTC", 50, 10), GoogleOAuth("gcid", "gsec", REDIRECT),
                    MicrosoftOAuth("mcid", "tenant-1"), (Tenant("lq", (GMAIL,)), Tenant("look", (GRAPH,)), Tenant("demo", (IMAP,))))
PROVIDERS = {"google": google_provider("gcid", "gsec"), "microsoft": microsoft_provider("mcid", "tenant-1")}
TOKEN = {"access_token": "at", "expires_in": 3600, "refresh_token": "rt-new"}
DEVICE = {"device_code": "dc", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin",
          "interval": 5, "expires_in": 900}


class FakeHttp:
    def __init__(self):
        self.post_responses, self.get_responses, self.posts, self.gets = [], [], [], []

    def post_form(self, url, data, headers=None):
        self.posts.append((url, data))
        status, body = self.post_responses.pop(0)
        if status >= 400:
            raise HttpError(status, body)
        return body

    def get_json(self, url, headers=None):
        self.gets.append((url, headers))
        return self.get_responses.pop(0)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    state = StateDb(tmp_path / "state.db")
    http = FakeHttp()
    tokens = TokenManager(state, PROVIDERS, http)
    spawned = []
    sleeps = []
    clock = Clock()
    flows = OAuthFlows(state, PROVIDERS, CONFIG, http, tokens, clock=clock, spawn=spawned.append, sleep=sleeps.append)
    return state, http, tokens, flows, spawned, sleeps, clock


def params_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# ---------- Google 授权码 ----------

def test_google_flow_start_callback_done(env):
    state, http, tokens, flows, spawned, _, _ = env
    view = flows.start("lq-ops@example.com")
    assert view.mode == "code" and view.user_code is None and view.expires_at == T0 + FLOW_TTL
    p = params_of(view.auth_url)
    assert p["client_id"] == "gcid" and p["redirect_uri"] == REDIRECT and p["login_hint"] == "lq-ops@example.com"
    assert p["code_challenge_method"] == "S256" and len(p["state"]) >= 32 and p["access_type"] == "offline"
    assert flows.status(view.flow_id) == {"status": "pending", "message": ""} and spawned == []
    http.post_responses = [(200, TOKEN)]
    http.get_responses = [{"emailAddress": "LQ-Ops@example.com"}]
    flows.complete_callback("the-code", p["state"])
    assert flows.status(view.flow_id) == {"status": "done", "message": ""}
    url, data = http.posts[0]
    assert data["code"] == "the-code" and data["redirect_uri"] == REDIRECT and data["client_secret"] == "gsec" and "code_verifier" in data
    row = state.get_token("lq-ops@example.com")
    assert (row.provider, row.account, row.refresh_token, row.status) == ("google", "lq-ops@example.com", "rt-new", "ok")
    assert [a.action for a in state.recent_audit()] == ["oauth_authorized"]
    assert state.recent_audit()[0].target == "lq-ops@example.com"


def test_state_must_match_and_is_single_use(env):
    _, http, _, flows, _, _, _ = env
    view = flows.start("lq-ops@example.com")
    with pytest.raises(FlowError):
        flows.complete_callback("c", "forged-state")
    with pytest.raises(FlowError):
        flows.complete_callback("c", "伪造的 state ✓")   # 非 ASCII 也是 400，不能 500
    http.post_responses = [(200, TOKEN)]
    http.get_responses = [{"emailAddress": "lq-ops@example.com"}]
    flows.complete_callback("c", params_of(view.auth_url)["state"])
    with pytest.raises(FlowError):
        flows.complete_callback("c", params_of(view.auth_url)["state"])  # 已消费


def test_pasted_redirect_url(env):
    state, http, _, flows, _, _, _ = env
    view = flows.start("lq-ops@example.com")
    st = params_of(view.auth_url)["state"]
    http.post_responses = [(200, TOKEN)]
    http.get_responses = [{"emailAddress": "lq-ops@example.com"}]
    flows.complete_pasted(view.flow_id, f"{REDIRECT}?state={st}&code=abc&scope=x")
    assert flows.status(view.flow_id)["status"] == "done" and state.get_token("lq-ops@example.com")
    with pytest.raises(FlowError):
        flows.complete_pasted(view.flow_id, "http://localhost:18090/api/mail/oauth/callback?error=access_denied")
    with pytest.raises(FlowError):
        flows.complete_pasted("no-such-flow", f"{REDIRECT}?state=x&code=y")


def test_account_mismatch_is_error_and_saves_nothing(env):
    state, http, _, flows, _, _, _ = env
    view = flows.start("lq-ops@example.com")
    http.post_responses = [(200, TOKEN)]
    http.get_responses = [{"emailAddress": "someone-else@example.com"}]
    flows.complete_callback("c", params_of(view.auth_url)["state"])
    s = flows.status(view.flow_id)
    assert s["status"] == "error" and "someone-else@example.com" in s["message"]
    assert state.get_token("lq-ops@example.com") is None and state.recent_audit() == []


def test_exchange_failure_and_missing_refresh_token(env):
    state, http, _, flows, _, _, _ = env
    view = flows.start("lq-ops@example.com")
    http.post_responses = [(400, {"error": "invalid_grant", "error_description": "Bad code"})]
    flows.complete_callback("c", params_of(view.auth_url)["state"])
    assert flows.status(view.flow_id) == {"status": "error", "message": "invalid_grant: Bad code"}
    view = flows.start("lq-ops@example.com")  # 出错后可以重新开始
    http.post_responses = [(200, {"access_token": "at", "expires_in": 3600})]
    http.get_responses = [{"emailAddress": "lq-ops@example.com"}]
    flows.complete_callback("c", params_of(view.auth_url)["state"])
    assert flows.status(view.flow_id)["status"] == "error" and "refresh token" in flows.status(view.flow_id)["message"]
    assert state.get_token("lq-ops@example.com") is None


# ---------- 流程表 ----------

def test_one_flow_per_mailbox_and_expiry(env):
    _, http, _, flows, _, _, clock = env
    a = flows.start("lq-ops@example.com")
    assert flows.start("lq-ops@example.com").flow_id == a.flow_id          # E26
    http.post_responses = [(200, DEVICE)]
    assert flows.start("ops@look.example").flow_id != a.flow_id
    clock.now = T0 + FLOW_TTL + timedelta(seconds=1)
    assert flows.status(a.flow_id) == {"status": "error", "message": "expired"}  # E4
    assert flows.start("lq-ops@example.com").flow_id != a.flow_id
    assert flows.status("unknown") == {"status": "error", "message": "unknown flow"}


def test_superseded_device_flow_stops_polling_and_saves_nothing(env):
    state, http, _, flows, spawned, _, clock = env
    http.post_responses = [(200, DEVICE)]
    first = flows.start("ops@look.example")
    clock.now = T0 + FLOW_TTL + timedelta(seconds=1)   # 第一个过期后再发起：第二个替换第一个
    http.post_responses = [(200, DEVICE)]
    second = flows.start("ops@look.example")
    assert second.flow_id != first.flow_id
    assert flows.status(first.flow_id) == {"status": "error", "message": "unknown flow"}
    http.post_responses = [(200, TOKEN)]
    spawned[0]()                                       # 旧流程的后台线程醒来：已被终止，不再请求 token 端点
    assert http.posts[-1][1].get("grant_type") != DEVICE_GRANT_TYPE and state.get_token("ops@look.example") is None
    assert flows.status(second.flow_id)["status"] == "pending"


def test_start_rejects_non_oauth_or_unknown_mailbox(env):
    _, _, _, flows, _, _, _ = env
    with pytest.raises(FlowError):
        flows.start("demo@example.com")
    with pytest.raises(FlowError):
        flows.start("nobody@example.com")


# ---------- Microsoft 设备码 ----------



def test_device_flow_polls_until_token(env):
    state, http, _, flows, spawned, sleeps, _ = env
    http.post_responses = [(200, DEVICE)]
    view = flows.start("ops@look.example")
    assert (view.mode, view.user_code, view.verification_uri, view.auth_url) == ("device", "ABCD-EFGH", "https://microsoft.com/devicelogin", None)
    assert http.posts[0][1] == {"client_id": "mcid", "scope": "offline_access Mail.ReadWrite User.Read"}
    assert len(spawned) == 1 and flows.status(view.flow_id)["status"] == "pending"
    http.post_responses = [(400, {"error": "authorization_pending"}), (400, {"error": "slow_down"}),
                           (500, {"error": "server_error"}), (200, TOKEN)]
    http.get_responses = [{"mail": "Ops@look.example", "userPrincipalName": "ops_look.example#EXT#@x"}]
    spawned[0]()
    assert sleeps == [5, 5, 10, 15]   # 首次等 interval；slow_down 后永久 +5；5xx 再 +5
    assert flows.status(view.flow_id) == {"status": "done", "message": ""}
    row = state.get_token("ops@look.example")
    assert (row.provider, row.account, row.refresh_token) == ("microsoft", "ops@look.example", "rt-new")
    assert all(d.get("grant_type") != "refresh_token" for _, d in http.posts)


def test_device_flow_terminal_error_and_expiry(env):
    state, http, _, flows, spawned, sleeps, clock = env
    http.post_responses = [(200, DEVICE), (400, {"error": "access_denied", "error_description": "User declined"})]
    view = flows.start("ops@look.example")
    spawned[0]()
    assert flows.status(view.flow_id) == {"status": "error", "message": "access_denied: User declined"}
    http.post_responses = [(200, DEVICE), (400, {"error": "authorization_pending"})]
    view = flows.start("ops@look.example")
    clock.now = T0 + FLOW_TTL + timedelta(seconds=1)
    spawned[1]()
    assert flows.status(view.flow_id) == {"status": "error", "message": "expired"} and state.get_token("ops@look.example") is None


def test_device_start_failure_is_flow_error(env):
    _, http, _, flows, spawned, _, _ = env
    http.post_responses = [(503, "unavailable")]
    with pytest.raises(FlowError):
        flows.start("ops@look.example")
    assert spawned == []


# ---------- Disconnect（D20 / E32）----------

def test_disconnect_is_local_only_and_cancels_inflight_flow(env):
    state, http, tokens, flows, spawned, _, _ = env
    state.save_token("ops@look.example", "microsoft", "ops@look.example", "rt-old")
    http.post_responses = [(200, {"access_token": "cached", "expires_in": 3600})]
    tokens.access_token("ops@look.example", "microsoft")
    http.post_responses = [(200, DEVICE)]
    view = flows.start("ops@look.example")
    posts_before = len(http.posts)
    flows.disconnect("ops@look.example")
    assert len(http.posts) == posts_before                         # 不调远程撤销
    assert state.get_token("ops@look.example") is None and tokens.auth_state("ops@look.example") == "not_authorized"
    assert flows.status(view.flow_id) == {"status": "error", "message": "cancelled"}
    assert [a.action for a in state.recent_audit()] == ["oauth_disconnected"]
    http.post_responses = [(200, TOKEN)]
    spawned[0]()                                                   # 后台轮询醒来：流程已取消，不再请求
    assert len(http.posts) == posts_before and state.get_token("ops@look.example") is None
    http.post_responses = [(200, {"access_token": "x", "expires_in": 3600})]
    with pytest.raises(Exception):
        tokens.access_token("ops@look.example", "microsoft")       # 内存 access token 已清，且无 refresh token
    assert len(http.posts) == posts_before


def test_generation_checked_before_saving(env):
    state, http, _, flows, _, _, _ = env
    view = flows.start("lq-ops@example.com")
    state.bump_generation("lq-ops@example.com")   # 流程进行中被断开（另一条路径）
    http.post_responses = [(200, TOKEN)]
    http.get_responses = [{"emailAddress": "lq-ops@example.com"}]
    flows.complete_callback("c", params_of(view.auth_url)["state"])
    assert flows.status(view.flow_id)["status"] == "error" and state.get_token("lq-ops@example.com") is None


def test_disconnect_without_token_is_noop_but_audited(env):
    state, _, _, flows, _, _, _ = env
    flows.disconnect("lq-ops@example.com")
    assert state.get_token("lq-ops@example.com") is None and state.get_generation("lq-ops@example.com") == 1
