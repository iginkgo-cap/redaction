import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from file_masker.mail import __main__ as cli
from file_masker.mail.oauth_flow import FlowError, FlowView

T0 = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


class FakeFlows:
    def __init__(self, view, statuses):
        self.view, self.statuses, self.calls = view, list(statuses), []

    def start(self, address):
        self.calls.append(("start", address))
        if isinstance(self.view, Exception):
            raise self.view
        return self.view

    def status(self, flow_id):
        self.calls.append(("status", flow_id))
        return self.statuses.pop(0)

    def complete_callback(self, code, state):
        self.calls.append(("callback", code, state))

    def disconnect(self, address):
        self.calls.append(("disconnect", address))


def test_parse_args():
    ns = cli.parse_args(["auth", "a@x.com"])
    assert (ns.command, ns.address, ns.disconnect, ns.port) == ("auth", "a@x.com", False, 18090)
    ns = cli.parse_args(["auth", "--disconnect", "a@x.com"])
    assert ns.disconnect
    ns = cli.parse_args(["auth", "a@x.com", "--port", "9999"])
    assert ns.port == 9999 and not ns.paste
    assert cli.parse_args(["auth", "a@x.com", "--paste"]).paste
    with pytest.raises(SystemExit):
        cli.parse_args(["auth"])


def test_disconnect(capsys):
    flows = FakeFlows(None, [])
    assert cli.run_auth(flows, "a@x.com", disconnect=True, port=0, sleep=lambda s: None) == 0
    assert flows.calls == [("disconnect", "a@x.com")] and "disconnected" in capsys.readouterr().out


def test_device_flow_prints_code_and_waits(capsys):
    view = FlowView("f1", "device", T0 + timedelta(minutes=10), None, "ABCD-EFGH", "https://microsoft.com/devicelogin")
    flows = FakeFlows(view, [{"status": "pending", "message": ""}, {"status": "done", "message": ""}])
    assert cli.run_auth(flows, "ops@look.example", disconnect=False, port=0, sleep=lambda s: None) == 0
    out = capsys.readouterr().out
    assert "ABCD-EFGH" in out and "https://microsoft.com/devicelogin" in out and "authorized" in out.lower()
    assert flows.calls[0] == ("start", "ops@look.example") and flows.calls[-1] == ("status", "f1")


def test_device_flow_error_returns_nonzero(capsys):
    view = FlowView("f1", "device", T0, None, "CODE", "https://x")
    flows = FakeFlows(view, [{"status": "error", "message": "access_denied: User declined"}])
    assert cli.run_auth(flows, "ops@look.example", disconnect=False, port=0, sleep=lambda s: None) == 1
    assert "access_denied" in capsys.readouterr().out


def test_google_flow_listens_for_callback(capsys):
    view = FlowView("f2", "code", T0 + timedelta(minutes=10), "https://accounts.google.com/o/oauth2/v2/auth?state=abc", None, None)
    flows = FakeFlows(view, [{"status": "pending", "message": ""}, {"status": "done", "message": ""}])
    server = cli.CallbackServer(flows, 0)   # 端口 0：随机空闲端口
    port = server.port
    t = threading.Thread(target=lambda: server.serve_until_hit(timeout=5), daemon=True)
    t.start()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/mail/oauth/callback?code=the-code&state=abc", timeout=5) as resp:
        assert resp.status == 200 and b"close this tab" in resp.read()
    t.join(5)
    assert ("callback", "the-code", "abc") in flows.calls
    assert cli.run_auth(flows, "lq@x.com", disconnect=False, port=0, sleep=lambda s: None, server=server) == 0
    out = capsys.readouterr().out
    assert "accounts.google.com" in out and "abc" in out


def test_callback_server_rejects_denied_and_invalid_state():
    flows = FakeFlows(None, [])
    flows.complete_callback = lambda code, state: (_ for _ in ()).throw(FlowError("bad state"))
    server = cli.CallbackServer(flows, 0)
    t = threading.Thread(target=lambda: server.serve_until_hit(timeout=5), daemon=True)
    t.start()
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/mail/oauth/callback?error=access_denied", timeout=5)
    assert e.value.code == 400
    t.join(5)
    assert server.result == "error"
    server = cli.CallbackServer(flows, 0)
    t = threading.Thread(target=lambda: server.serve_until_hit(timeout=5), daemon=True)
    t.start()
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/mail/oauth/callback?code=c&state=s", timeout=5)
    assert e.value.code == 400
    t.join(5)


def test_google_flow_pastes_when_port_is_busy(capsys):
    view = FlowView("f3", "code", T0 + timedelta(minutes=10), "https://accounts.google.com/x?state=abc", None, None)
    flows = FakeFlows(view, [{"status": "done", "message": ""}])
    flows.complete_pasted = lambda flow_id, url: flows.calls.append(("pasted", flow_id, url))
    busy = cli.CallbackServer(flows, 0)   # 占住端口，模拟服务本身在监听
    code = cli.run_auth(flows, "lq@x.com", disconnect=False, port=busy.port, sleep=lambda s: None,
                        read_line=lambda prompt: "http://localhost/api/mail/oauth/callback?code=x&state=abc")
    assert code == 0 and ("pasted", "f3", "http://localhost/api/mail/oauth/callback?code=x&state=abc") in flows.calls
    assert "paste" in capsys.readouterr().out.lower()


def test_paste_flag_skips_listener(capsys):
    view = FlowView("f4", "code", T0 + timedelta(minutes=10), "https://accounts.google.com/x?state=abc", None, None)
    flows = FakeFlows(view, [{"status": "done", "message": ""}])
    flows.complete_pasted = lambda flow_id, url: flows.calls.append(("pasted", flow_id, url))
    code = cli.run_auth(flows, "lq@x.com", disconnect=False, port=0, sleep=lambda s: None, paste=True,
                        read_line=lambda prompt: "http://localhost/cb?code=x&state=abc")
    assert code == 0 and ("pasted", "f4", "http://localhost/cb?code=x&state=abc") in flows.calls
    assert "Listening" not in capsys.readouterr().out


def test_start_failure_returns_nonzero(capsys):
    flows = FakeFlows(FlowError("uses password login"), [])
    assert cli.run_auth(flows, "demo@x.com", disconnect=False, port=0, sleep=lambda s: None) == 1
    assert "password login" in capsys.readouterr().out
