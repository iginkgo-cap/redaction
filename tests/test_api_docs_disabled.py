"""LOOK-6873：接口文档不对外（/docs、/redoc、/openapi.json 均 404），业务接口不受影响。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.settings, "detectors", "cn_regex,en_regex")
    with TestClient(main.app) as c:
        yield c
    main._DETECTORS.clear()
    main._ACTIVE_DETECTOR_NAMES.clear()


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_api_docs_not_exposed(client, path):
    assert client.get(path).status_code == 404


def test_business_endpoint_still_works(client):
    r = client.post("/v1/detect", json={"text": "手机 13800138000"})
    assert r.status_code == 200
