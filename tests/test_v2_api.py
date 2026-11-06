"""v2 接口（进程内 TestClient，只装正则识别器 + 可控的假模型识别器，不加载真模型）。

需要库的用例连 REDACTION_TEST_DATABASE_URL，没配就跳过。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import main
from app.detectors.base import Detector
from app.v2.store import NamespaceStore

from .conftest import reset_test_databases

KEY = base64.b64encode(b"k" * 32).decode()
DB_URL = os.environ.get("REDACTION_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="REDACTION_TEST_DATABASE_URL 未设置")
LABS, OTHER, OPS = "t-labs", "t-other", "t-ops"


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


NAMESPACES_YAML = f"""
callers:
  labs: {{token_sha256: ["{_sha(LABS)}"]}}
  other: {{token_sha256: ["{_sha(OTHER)}"]}}
  ops: {{token_sha256: ["{_sha(OPS)}"], admin: true}}
namespaces:
  - {{id: n1, slug: tenant-one, callers: [labs]}}
  - {{id: n2, slug: tenant-two, callers: [labs]}}
  - {{id: gone, slug: gone, callers: [labs]}}
  - {{id: n3, slug: tenant-three, callers: [other]}}
"""


class FakeModel(Detector):
    """假模型识别器：把文本里的「张三」「老王」识别成 PERSON。mode 控制失败方式。"""

    name = "fakener"
    heavy = True

    def __init__(self, mode: str = "ok"):
        self.mode = mode

    @property
    def ready(self) -> bool:
        return self.mode != "loading"

    async def detect(self, text):
        if self.mode == "timeout":
            await asyncio.sleep(1)
        if self.mode == "error":
            raise RuntimeError("boom")
        # context：只认「客户」后面的张三（模拟模型依赖上下文、同值别处漏识别）
        pattern = "(?<=客户)张三" if self.mode == "context" else "张三丰|张三|老王"
        return [
            {"start": m.start(), "end": m.end(), "text": m.group(), "canonical": "PERSON",
             "raw_label": "person", "source": self.name, "score": 0.9}
            for m in re.finditer(pattern, text)
        ]


def _start(monkeypatch, db: bool, tmp_path=None):
    monkeypatch.setattr(main.settings, "detectors", "cn_regex,en_regex")
    monkeypatch.setattr(main.settings, "detector_timeout_sec", 0.2)
    if db:
        monkeypatch.setattr(main.settings, "database_url", DB_URL)
        monkeypatch.setattr(main.settings, "encryption_key", KEY)
        monkeypatch.setattr(main.settings, "hmac_key", KEY)
        f = tmp_path / "namespaces.yaml"
        f.write_text(NAMESPACES_YAML)
        monkeypatch.setattr(main.settings, "namespaces_file", f)
        reset_test_databases(DB_URL)
        return TestClient(main.app, headers={"X-Redaction-Token": LABS})
    monkeypatch.setattr(main.settings, "database_url", None)
    return TestClient(main.app)


def _with_model(mode: str = "ok") -> None:
    main._DETECTORS["fakener"] = FakeModel(mode)
    main._ACTIVE_DETECTOR_NAMES.append("fakener")


@pytest.fixture()
def nodb(monkeypatch):
    with _start(monkeypatch, db=False) as c:
        yield c
    main._DETECTORS.clear()
    main._ACTIVE_DETECTOR_NAMES.clear()


@pytest.fixture()
def db(monkeypatch, tmp_path):
    if not DB_URL:
        pytest.skip("REDACTION_TEST_DATABASE_URL 未设置")
    with _start(monkeypatch, db=True, tmp_path=tmp_path) as c:
        _with_model()
        yield c
    main._DETECTORS.clear()
    main._ACTIVE_DETECTOR_NAMES.clear()


def _redact(c, ns="n1", **kw):
    body = {"namespace": ns, "items": [{"id": "m1", "text": kw.pop("text", "帮我查张三的持仓，电话13800138000")}], **kw}
    return c.post("/v2/redact", json=body)


# ── 无库模式 ──────────────────────────────────────────────

def test_无库_detect_可用且任何_namespace_都接受(nodb) -> None:
    r = nodb.post("/v2/detect", json={"namespace": "anything", "items": [{"id": "m1", "text": "电话13800138000"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["spans"][0]["kind"] == "PHONE"
    assert body["degraded"] is False and body["applied_level"] == "full"
    assert nodb.get("/version").json()["storage"] == "none"


def test_无库_redact_invalid_request_其余_storage_unavailable(nodb) -> None:
    r = _redact(nodb)
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"
    assert r.headers["X-Redaction-Error"] == "invalid_request"
    for path, body in [
        ("/v2/restore", {"namespace": "x", "items": [{"id": "a", "text": "[[PERSON_1]]"}]}),
        ("/v2/mappings/lookup", {"namespace": "x", "placeholders": ["[[PERSON_1]]"]}),
        ("/v2/namespaces/remove", {"namespace": "x", "confirm": "x"}),
    ]:
        r = nodb.post(path, json=body)
        assert r.status_code == 503 and r.json()["code"] == "storage_unavailable", path


def test_校验失败_400_且拒绝_markdown_aware(nodb) -> None:
    ok = {"namespace": "x", "items": [{"id": "m1", "text": "hi"}]}
    for bad in [{**ok, "markdown_aware": True}, {"items": ok["items"]}, {**ok, "items": [{"id": "m1"}]},
                {**ok, "exclude": {"kinds": ["NOPE"]}}, {**ok, "exclude": {"values": [{"value": "x"}]}}]:
        r = nodb.post("/v2/detect", json=bad)
        assert r.status_code == 400 and r.json()["code"] == "invalid_request", bad
    # /v1 的校验错误仍是 FastAPI 默认 422
    assert nodb.post("/v1/detect", json={}).status_code == 422


def test_总字数超限_413(nodb, monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "max_request_chars", 10)
    r = nodb.post("/v2/detect", json={"namespace": "x", "items": [{"id": "a", "text": "123456"},
                                                                   {"id": "b", "json": {"k": "123456"}}]})
    assert r.status_code == 413 and r.json()["code"] == "text_too_large"


def test_detect_不碰已有占位符_且_json_带_path(nodb) -> None:
    _with_model()
    r = nodb.post("/v2/detect", json={"namespace": "x", "items": [
        {"id": "m1", "text": "[[PERSON_1]] 和张三"},
        {"id": "t1", "json": {"a": ["老王", 1], "张三": True}}]})
    items = r.json()["items"]
    assert [(s["text"], s["start"]) for s in items[0]["spans"]] == [("张三", 14)]
    assert [(s["text"], s["path"]) for s in items[1]["spans"]] == [("老王", "$.a[0]")]


# ── 降级 ─────────────────────────────────────────────────

@pytest.mark.parametrize("mode,reason", [("timeout", "detector_timeout"), ("error", "detector_error"),
                                         ("loading", "not_ready")])
def test_模型失败_降级到正则(nodb, mode, reason) -> None:
    _with_model(mode)
    r = nodb.post("/v2/detect", json={"namespace": "x", "items": [{"id": "m1", "text": "张三 13800138000"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True and body["applied_level"] == "regex" and body["degrade_reason"] == reason
    assert r.headers["X-Redaction-Degraded"] == "regex"
    assert [s["kind"] for s in body["items"][0]["spans"]] == ["PHONE"]
    if mode != "loading":
        assert body["detectors_failed"] == ["fakener"]


def test_排队满_降级到正则(nodb, monkeypatch) -> None:
    _with_model()
    monkeypatch.setattr(main._LIMITER, "max_pending", 0)
    r = nodb.post("/v2/detect", json={"namespace": "x", "items": [{"id": "m1", "text": "张三 13800138000"}]})
    body = r.json()
    assert body["degraded"] is True and body["degrade_reason"] == "overloaded"
    assert [s["kind"] for s in body["items"][0]["spans"]] == ["PHONE"]


@pytest.mark.parametrize("mode,code,status", [("timeout", "detect_failed", 500), ("error", "detect_failed", 500),
                                              ("loading", "not_ready", 503), ("overloaded", "overloaded", 503)])
def test_allow_degrade_false_直接报错(nodb, monkeypatch, mode, code, status) -> None:
    _with_model("ok" if mode == "overloaded" else mode)
    if mode == "overloaded":
        monkeypatch.setattr(main._LIMITER, "max_pending", 0)
    r = nodb.post("/v2/detect", json={"namespace": "x", "allow_degrade": False,
                                      "items": [{"id": "m1", "text": "张三"}]})
    assert r.status_code == status and r.json()["code"] == code
    assert r.headers["X-Redaction-Error"] == code


def test_正则识别器失败_detect_failed(nodb, monkeypatch) -> None:
    async def boom(text):
        raise RuntimeError("bad yaml")

    monkeypatch.setattr(main._DETECTORS["cn_regex"], "detect", boom)
    r = nodb.post("/v2/detect", json={"namespace": "x", "items": [{"id": "m1", "text": "13800138000"}]})
    assert r.status_code == 500 and r.json()["code"] == "detect_failed"


def test_level_regex_不跑模型_不算降级(nodb) -> None:
    _with_model("error")
    r = nodb.post("/v2/detect", json={"namespace": "x", "level": "regex",
                                      "items": [{"id": "m1", "text": "张三 13800138000"}]})
    body = r.json()
    assert body["degraded"] is False and body["applied_level"] == "regex"
    assert "X-Redaction-Degraded" not in r.headers


# ── 有库 ─────────────────────────────────────────────────

@needs_db
def test_redact_同值同占位符_跨请求跨项(db) -> None:
    r1 = _redact(db).json()
    assert r1["items"][0]["text"] == "帮我查[[PERSON_1]]的持仓，电话[[PHONE_1]]"
    e = {x["placeholder"]: x for x in r1["entities"]}
    assert e["[[PERSON_1]]"]["new"] is True and e["[[PERSON_1]]"]["occurrences"] == [{"item": "m1", "start": 3, "end": 5}]
    r2 = db.post("/v2/redact", json={"namespace": "n1", "items": [
        {"id": "m2", "text": "张三和老王"},
        {"id": "t1", "json": {"title": "张三的会议", "attendees": ["张三", "老王"], "count": 2}}]}).json()
    assert r2["items"][0]["text"] == "[[PERSON_1]]和[[PERSON_2]]"
    assert r2["items"][1]["json"] == {"title": "[[PERSON_1]]的会议", "attendees": ["[[PERSON_1]]", "[[PERSON_2]]"],
                                      "count": 2}
    e2 = {x["placeholder"]: x for x in r2["entities"]}
    assert e2["[[PERSON_1]]"]["new"] is False and e2["[[PERSON_2]]"]["new"] is True
    assert {"item": "t1", "path": "$.attendees[0]", "start": 0, "end": 2} in e2["[[PERSON_1]]"]["occurrences"]
    assert db.get("/version").json()["storage"] == "postgres"


@needs_db
def test_跨_namespace_不可见(db) -> None:
    _redact(db, "n1")
    assert _redact(db, "n2", text="老王").json()["items"][0]["text"] == "[[PERSON_1]]"
    r = db.post("/v2/restore", json={"namespace": "n2", "items": [{"id": "a", "text": "[[PERSON_1]] [[PHONE_1]]"}]})
    assert r.json() == {"items": [{"id": "a", "text": "老王 [[PHONE_1]]"}], "unresolved": ["[[PHONE_1]]"]}
    r = db.post("/v2/mappings/lookup", json={"namespace": "n1", "placeholders": ["[[PERSON_1]]", "[[PERSON_9]]"]})
    assert r.json() == {"mappings": [{"placeholder": "[[PERSON_1]]", "value": "张三", "kind": "PERSON"}],
                        "unresolved": ["[[PERSON_9]]"]}


@needs_db
def test_restore_容忍转义_与_redact_kind(db) -> None:
    _redact(db)
    r = db.post("/v2/restore", json={"namespace": "n1", "on_unresolved": "redact_kind",
                                     "items": [{"id": "a", "text": r"[[PERSON\_1]]的电话[[PHONE_1]]，[[ORG_3]]"}]})
    assert r.json() == {"items": [{"id": "a", "text": "张三的电话13800138000，[REDACTED:ORG]"}],
                        "unresolved": ["[[ORG_3]]"]}


@needs_db
def test_误识别反馈只影响本_namespace_include_可撤销(db) -> None:
    r = _redact(db, exclude={"values": [{"value": "张三", "reason": "false_positive"}]})
    assert r.json()["items"][0]["text"] == "帮我查张三的持仓，电话[[PHONE_1]]"
    assert _redact(db).json()["items"][0]["text"].startswith("帮我查张三的")  # 本 namespace 以后自动跳过
    assert _redact(db, "n2").json()["items"][0]["text"].startswith("帮我查[[PERSON_1]]")  # 别的 namespace 不受影响
    r = _redact(db, include={"values": [{"value": "张三", "kind": "PERSON"}]}).json()
    assert r["items"][0]["text"].startswith("帮我查[[PERSON_1]]")
    assert _redact(db).json()["items"][0]["text"].startswith("帮我查[[PERSON_1]]")  # 抑制已清除


@needs_db
def test_不带_reason_的排除只本次生效_exclude_kinds(db) -> None:
    assert _redact(db, exclude={"values": [{"value": "张三"}]}).json()["items"][0]["text"].startswith("帮我查张三")
    assert _redact(db).json()["items"][0]["text"].startswith("帮我查[[PERSON_1]]")
    r = _redact(db, exclude={"kinds": ["PHONE"]}).json()
    assert r["items"][0]["text"] == "帮我查[[PERSON_1]]的持仓，电话13800138000"


@needs_db
def test_include_补漏识别_与_known_placeholders(db) -> None:
    r = _redact(db, text="客户周大福来电", include={"values": [{"value": "周大福", "kind": "CUSTOM"}]}).json()
    assert r["items"][0]["text"] == "客户[[CUSTOM_1]]来电"
    assert r["entities"][0]["source"] == "include"
    r = _redact(db, text="周大福又来电", level="none", known_placeholders=["[[CUSTOM_1]]", "[[PERSON_404]]"]).json()
    assert r["items"][0]["text"] == "[[CUSTOM_1]]又来电"
    assert r["entities"][0]["new"] is False


@needs_db
def test_已有占位符原样保留(db) -> None:
    text = r"[[PERSON_7]]和张三，[PERSON_8] \[PERSON\_9\] [REDACTED:PERSON] PERSON"
    r = _redact(db, text=text, include={"values": [{"value": "PERSON", "kind": "CUSTOM"}]}).json()
    assert r["items"][0]["text"] == r"[[PERSON_7]]和[[PERSON_1]]，[PERSON_8] \[PERSON\_9\] [REDACTED:PERSON] [[CUSTOM_1]]"


@needs_db
def test_known_placeholders_长值优先_英文词边界(db) -> None:
    _redact(db, text="张三")
    r = _redact(db, text="张三丰来了", known_placeholders=["[[PERSON_1]]"]).json()
    assert r["items"][0]["text"] == "[[PERSON_2]]来了"  # 不切成 [[PERSON_1]]丰
    _redact(db, text="Li", include={"values": [{"value": "Li", "kind": "CUSTOM"}]})
    r = _redact(db, text="Li of Limited", level="none", known_placeholders=["[[CUSTOM_1]]"]).json()
    assert r["items"][0]["text"] == "[[CUSTOM_1]] of Limited"


@needs_db
def test_未登记_namespace_被拒(db) -> None:
    r = _redact(db, "unknown")
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"


@needs_db
def test_删除_namespace_只有_admin_调用方能调(db) -> None:
    _redact(db, "gone")
    body = {"namespace": "gone", "confirm": "gone"}
    r = db.post("/v2/namespaces/remove", json=body)  # labs 不是 admin
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    r = db.post("/v2/namespaces/remove", json=body, headers={"X-Redaction-Token": "wrong"})
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"
    ops = {"X-Redaction-Token": OPS}
    assert db.post("/v2/namespaces/remove", json={**body, "confirm": "n1"}, headers=ops).status_code == 400
    assert _redact(db, "gone").status_code == 200  # 以上都没删
    r = db.post("/v2/namespaces/remove", json=body, headers=ops)
    assert r.json() == {"namespace": "gone", "removed_entities": 2}
    assert _redact(db, "gone").status_code == 400
    assert db.post("/v2/namespaces/remove", json=body, headers=ops).json()["removed_entities"] == 0
    assert _redact(db, "n1").status_code == 200


@needs_db
@pytest.mark.parametrize("path,body", [
    ("/v2/detect", {"namespace": "n1", "items": [{"id": "a", "text": "x"}]}),
    ("/v2/redact", {"namespace": "n1", "items": [{"id": "a", "text": "x"}]}),
    ("/v2/restore", {"namespace": "n1", "items": [{"id": "a", "text": "[[PERSON_1]]"}]}),
    ("/v2/mappings/lookup", {"namespace": "n1", "placeholders": ["[[PERSON_1]]"]}),
])
def test_有库部署每个接口都要令牌(db, path, body) -> None:
    for headers in [{"X-Redaction-Token": ""}, {"X-Redaction-Token": "wrong"}]:
        r = db.post(path, json=body, headers=headers)
        assert r.status_code == 401 and r.json()["code"] == "unauthorized", (path, headers)
        assert r.headers["X-Redaction-Error"] == "unauthorized"
    # 令牌有效但没授权这个 namespace
    r = db.post(path, json={**body, "namespace": "n3"})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert "tenant-three" not in r.json()["message"]  # 不向别的调用方透露 id 对应哪个客户
    assert db.post(path, json=body).status_code == 200


@needs_db
def test_调用方只能看到授权给它的_namespace(db) -> None:
    other = {"X-Redaction-Token": OTHER}
    r = db.post("/v2/redact", headers=other, json={"namespace": "n3", "items": [{"id": "a", "text": "张三"}]})
    assert r.json()["items"][0]["text"] == "[[PERSON_1]]"
    _redact(db, "n1", text="老王")
    # labs 拿不到 n3 的映射；other 拿不到 n1 的
    assert db.post("/v2/mappings/lookup", json={"namespace": "n3", "placeholders": ["[[PERSON_1]]"]}).status_code == 403
    r = db.post("/v2/mappings/lookup", headers=other, json={"namespace": "n1", "placeholders": ["[[PERSON_1]]"]})
    assert r.status_code == 403
    r = db.post("/v2/mappings/lookup", headers=other, json={"namespace": "n3", "placeholders": ["[[PERSON_1]]"]})
    assert r.json()["mappings"][0]["value"] == "张三"


@needs_db
def test_数据落在各自的库里(db) -> None:
    _redact(db, "n1")
    _redact(db, "n2", text="老王")
    with psycopg.connect(DB_URL) as conn:
        dbs = dict(conn.execute("SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database "
                                "WHERE datname LIKE 'rs\\_%'").fetchall())
    assert dbs == {"rs_n1": "tenant-one", "rs_n2": "tenant-two"}


@needs_db
def test_库不可达_storage_unavailable(db, monkeypatch) -> None:
    async def down(*a, **kw):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(NamespaceStore, "suppressed", down)
    r = _redact(db)
    assert r.status_code == 503 and r.json()["code"] == "storage_unavailable"
    assert r.headers["Retry-After"] == "1"


async def test_单个请求同时推理的文本段不超过上限() -> None:
    from app.parallel import InferenceLimiter
    from app.v2.detection import detect_texts

    state = {"running": 0, "peak": 0}

    class Counting(FakeModel):
        async def detect(self, text):
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.01)
            state["running"] -= 1
            return []

    await detect_texts(["leaf"] * 20, level="full", allow_degrade=True, detectors=[Counting()],
                       limiter=InferenceLimiter(4, 6), timeout_sec=5)
    assert state["peak"] == 2
