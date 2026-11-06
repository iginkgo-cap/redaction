"""LOOK-5231：/v1 不鉴权，客户版用 REDACTION_ENABLE_V1=false 关掉；内部默认开（labs 还在用，见 LOOK-6877）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main

V1_CALLS = [
    ("/v1/detect", {"text": "手机 13800138000"}),
    ("/v1/redact", {"text": "手机 13800138000"}),
    ("/v1/restore", {"text": "[[PHONE_1]]", "mapping": []}),
]


def _client(monkeypatch, enable_v1: bool):
    monkeypatch.setattr(main.settings, "detectors", "cn_regex,en_regex")
    monkeypatch.setattr(main.settings, "enable_v1", enable_v1)
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_detectors():
    yield
    main._DETECTORS.clear()
    main._ACTIVE_DETECTOR_NAMES.clear()


def test_enable_v1_defaults_to_true():
    assert main.settings.enable_v1 is True


@pytest.mark.parametrize(("path", "body"), V1_CALLS)
def test_v1_available_when_enabled(monkeypatch, path, body):
    with _client(monkeypatch, enable_v1=True) as c:
        assert c.post(path, json=body).status_code == 200


@pytest.mark.parametrize(("path", "body"), V1_CALLS)
def test_v1_not_found_when_disabled(monkeypatch, path, body):
    with _client(monkeypatch, enable_v1=False) as c:
        assert c.post(path, json=body).status_code == 404


def test_health_unaffected_when_v1_disabled(monkeypatch):
    with _client(monkeypatch, enable_v1=False) as c:
        assert c.get("/health").status_code == 200
