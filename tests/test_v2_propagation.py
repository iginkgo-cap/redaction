"""v2 同值扩散（LOOK-6876）：同一请求里识别出的值，其他出现位置一并替换（不论是否被识别器命中）。

复用 test_v2_api 的 fixture；FakeModel 的 context 模式只认「客户」后面的张三，模拟模型依赖上下文漏识别。
"""
from __future__ import annotations

from app import main

from .test_v2_api import db  # noqa: F401  fixture


def _context_model() -> None:
    main._DETECTORS["fakener"].mode = "context"


def test_同一段里识别出的值_别处出现也替换(db) -> None:
    _context_model()
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [
        {"id": "m1", "text": "客户张三的结单；备注：张三已确认"}]})

    body = r.json()
    ph = body["entities"][0]["placeholder"]
    assert body["items"][0]["text"] == f"客户{ph}的结单；备注：{ph}已确认"
    assert len(body["entities"][0]["occurrences"]) == 2


def test_跨项也扩散_没被识别的项里同值替换成同一占位符(db) -> None:
    _context_model()
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [
        {"id": "m1", "text": "客户张三"}, {"id": "m2", "text": "张三的电话"}]})

    body = r.json()
    ph = body["entities"][0]["placeholder"]
    assert [it["text"] for it in body["items"]] == [f"客户{ph}", f"{ph}的电话"]
    assert sorted(o["item"] for o in body["entities"][0]["occurrences"]) == ["m1", "m2"]


def test_被抑制或排除的值不扩散(db) -> None:
    _context_model()
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [
        {"id": "m1", "text": "客户张三；备注：张三"}],
        "exclude": {"values": [{"value": "张三", "reason": "false_positive"}]}})

    body = r.json()
    assert body["items"][0]["text"] == "客户张三；备注：张三"
    assert body["entities"] == []


def test_扩散沿用已知值规则_中文按子串匹配(db) -> None:
    _context_model()
    r = db.post("/v2/redact", json={"namespace": "n1", "items": [
        {"id": "m1", "text": "客户张三；张三丰另说"}]})

    body = r.json()
    ph = body["entities"][0]["placeholder"]
    # 与 known_placeholders 同一套 find_known：中文无词边界，较长的「张三丰」里的「张三」也会被换掉
    assert body["items"][0]["text"] == f"客户{ph}；{ph}丰另说"
