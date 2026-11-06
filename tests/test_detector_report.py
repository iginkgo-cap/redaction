"""LOOK-6827 F'：/v1/detect 的 detectors_run / detectors_failed / detectors_skipped。

进程内 TestClient，只装正则 detector；HanLP / GLiNER 用真实 detector 类配假模型，
覆盖逐 chunk 失败的汇报路径，不加载真实模型。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main
from app.detectors import hanlp
from app.detectors.base import Detector
from app.detectors.gliner_generic import GlinerDetector
from app.detectors.hanlp import HanlpDetector

ZH_TEXT = "客户张三今天来电咨询基金净值。坏数据坏数据坏数据坏数据坏数据。"
EN_TEXT = "Please call John Smith today. BAD BAD BAD BAD BAD BAD."


class _FakeHanlpModel:
    """逐字分词；含「坏」的 chunk 抛错，其余 chunk 识别出「张三」。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, text, tasks):
        self.calls += 1
        if "坏" in text:
            raise RuntimeError("simulated chunk failure")
        ner = []
        idx = text.find("张三")
        if idx >= 0:
            ner.append(("张三", "PERSON", idx, idx + 2))
        return {"tok/fine": list(text), "ner/ontonotes": ner}


class _FakeGlinerModel:
    """含 BAD 的 chunk 抛错，其余 chunk 识别出 John Smith。"""

    def predict_entities(self, text, labels, threshold):
        if "BAD" in text:
            raise RuntimeError("simulated chunk failure")
        idx = text.find("John Smith")
        if idx < 0:
            return []
        return [{"label": "person name", "start": idx, "end": idx + 10, "score": 0.9}]


class _SlowDetector(Detector):
    name = "slow"

    def __init__(self):
        pass

    async def detect(self, text):
        await asyncio.sleep(2)
        return []


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.settings, "detectors", "cn_regex,en_regex")
    monkeypatch.setattr(main.settings, "hanlp_chunk_chars", 20)
    monkeypatch.setattr(main.settings, "gliner_chunk_chars", 30)
    monkeypatch.setattr(main.settings, "gliner_chunk_overlap", 0)
    with TestClient(main.app) as c:
        yield c
    main._DETECTORS.clear()
    main._ACTIVE_DETECTOR_NAMES.clear()


def _add(det: Detector) -> None:
    main._DETECTORS[det.name] = det
    main._ACTIVE_DETECTOR_NAMES.append(det.name)


def _hanlp(monkeypatch, model) -> HanlpDetector:
    monkeypatch.setattr(hanlp, "_MODEL", model)
    return HanlpDetector(main._DETECTORS["canonical"])


def _gliner(model) -> GlinerDetector:
    det = GlinerDetector(main._DETECTORS["canonical"], name="knowledgator", model_repo="fake")
    det._model = model
    return det


def _detect(client, text):
    r = client.post("/v1/detect", json={"text": text})
    assert r.status_code == 200
    return r.json()


def _persons(body):
    return {s["text"] for s in body["spans"] if s["canonical"] == "PERSON"}


def test_全部成功_三个列表(client, monkeypatch):
    _add(_hanlp(monkeypatch, _FakeHanlpModel()))
    _add(_gliner(_FakeGlinerModel()))
    body = _detect(client, "客户张三今天来电咨询基金净值。手机 13800138000")
    assert body["detectors_run"] == ["cn_regex", "en_regex", "hanlp", "knowledgator"]
    assert body["detectors_failed"] == []
    assert body["detectors_skipped"] == []
    assert "张三" in _persons(body)


def test_hanlp_单_chunk_失败_进_failed_且保留其他_chunk_结果(client, monkeypatch):
    model = _FakeHanlpModel()
    _add(_hanlp(monkeypatch, model))
    body = _detect(client, ZH_TEXT)
    assert model.calls == 2  # 两个 chunk，第二个失败
    assert body["detectors_run"] == ["cn_regex", "en_regex"]
    assert body["detectors_failed"] == ["hanlp"]
    assert body["detectors_skipped"] == []
    assert "张三" in _persons(body)


def test_gliner_单_chunk_失败_进_failed(client, monkeypatch):
    _add(_hanlp(monkeypatch, _FakeHanlpModel()))
    _add(_gliner(_FakeGlinerModel()))
    body = _detect(client, EN_TEXT)
    assert body["detectors_failed"] == ["knowledgator"]
    assert "knowledgator" not in body["detectors_run"]
    assert "John Smith" in _persons(body)


def test_detector_超时_进_failed(client, monkeypatch):
    monkeypatch.setattr(main.settings, "detector_timeout_sec", 0.2)
    _add(_SlowDetector())
    body = _detect(client, "手机 13800138000")
    assert body["detectors_run"] == ["cn_regex", "en_regex"]
    assert body["detectors_failed"] == ["slow"]
    assert body["detectors_skipped"] == []


def test_hanlp_汉字不足阈值_进_skipped_且仍在_run(client, monkeypatch):
    model = _FakeHanlpModel()
    _add(_hanlp(monkeypatch, model))
    body = _detect(client, "Please call John Smith today.")
    assert model.calls == 0
    assert body["detectors_run"] == ["cn_regex", "en_regex", "hanlp"]
    assert body["detectors_failed"] == []
    assert body["detectors_skipped"] == ["hanlp"]
