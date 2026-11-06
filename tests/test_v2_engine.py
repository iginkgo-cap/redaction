"""v2 engine 纯函数：选 span、占位符保护、已知值匹配。"""
from __future__ import annotations

from app.v2 import engine


def _c(start: int, end: int, priority: int, kind: str = "PERSON") -> dict:
    return {"start": start, "end": end, "kind": kind, "priority": priority}


def test_重叠时长的优先_不把长实体切碎() -> None:
    t = "地址：北京市张三路1号"
    picked = engine.select(t, [_c(3, 11, engine.DETECTED, "ADDRESS"), _c(6, 8, engine.INCLUDE)])
    assert [(c["start"], c["end"], c["kind"]) for c in picked] == [(3, 11, "ADDRESS")]
    t = "张三丰来了"
    picked = engine.select(t, [_c(0, 3, engine.DETECTED), _c(0, 2, engine.KNOWN)])
    assert [(c["start"], c["end"]) for c in picked] == [(0, 3)]


def test_等长时按优先级() -> None:
    picked = engine.select("张三", [_c(0, 2, engine.DETECTED, "ORG"), _c(0, 2, engine.KNOWN)])
    assert [c["kind"] for c in picked] == ["PERSON"]


def test_占位符变形写法与降级标记都受保护() -> None:
    for t in ["[[PERSON_4]]", r"[[PERSON\_4]]", "[PERSON_4]", r"\[PERSON\_4\]", "[REDACTED:PERSON]"]:
        assert set(engine.placeholder_regions(t)) == {(0, len(t))}, t
    assert engine.placeholder_regions("[备注] [PERSON] [FOO_1]") == []


def test_已知值_词边界与最短长度() -> None:
    assert engine.find_known("Li and Limited, MLi, Li.", "Li") == [(0, 2), (21, 23)]
    assert engine.find_known("张三丰和张三", "张三") == [(0, 2), (4, 6)]  # 中文不要求词边界
    assert engine.find_known("a X b", "X") == []
