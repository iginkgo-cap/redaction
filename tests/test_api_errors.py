"""LOOK-6827：过载 503 机器码与 /ready 就绪语义（进程内 TestClient，不加载模型）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(monkeypatch):
    # 只装正则 detector，不加载模型；用 monkeypatch 改 settings，避免污染其他测试读到的默认 profile
    monkeypatch.setattr(main.settings, "detectors", "cn_regex,en_regex")
    with TestClient(main.app) as c:
        yield c
    main._DETECTORS.clear()
    main._ACTIVE_DETECTOR_NAMES.clear()


def test_detect_ok_with_regex_only(client):
    r = client.post("/v1/detect", json={"text": "手机 13800138000"})
    assert r.status_code == 200
    assert any(s["canonical"] == "PHONE" for s in r.json()["spans"])


@pytest.mark.parametrize("path", ["/v1/detect", "/v1/redact"])
def test_overloaded_503_carries_machine_code(client, monkeypatch, path):
    monkeypatch.setattr(main._LIMITER, "max_pending", 0)
    r = client.post(path, json={"text": "手机 13800138000"})
    assert r.status_code == 503
    assert r.headers.get("X-Redaction-Error") == "overloaded"
    assert r.headers.get("Retry-After") == "1"
    detail = r.json()["detail"]
    assert detail["code"] == "overloaded"
    assert detail["code"] == r.headers.get("X-Redaction-Error")


def test_ready_200_when_all_detectors_ready(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_ready_503_when_a_detector_is_loading(client, monkeypatch):
    class Loading:
        ready = False

    monkeypatch.setitem(main._DETECTORS, "en_regex", Loading())
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "loading"
    assert body["detectors"]["en_regex"] == "loading"


def test_ready_503_before_detectors_registered(client, monkeypatch):
    monkeypatch.setattr(main, "_ACTIVE_DETECTOR_NAMES", [])
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "loading"
