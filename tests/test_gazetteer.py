"""gazetteer.py + detectors/gazetteer.py 单测（round11 S7 英文名双向修）。

覆盖：
- 拼音音节校验：合法拼音名通过 / 非拼音英文词拒绝
- gate 方向 is_known_person_token：名单 / 拼音 / 未知
- recall 方向 recall_eligible：歧义名（May/Will）排除
- GazetteerDetector：补召回命中、相邻缝合、歧义名不出 span、小写词不出 span
- precision S7 gate 端到端：未知单名 drop、上下文救回、多词名不受影响
"""
from __future__ import annotations

import pytest

from app.detectors.gazetteer import GazetteerDetector
from app.gazetteer import Gazetteer, get_gazetteer, is_pinyin_name
from app.precision import apply_precision_rules, SuppressionList

_EMPTY = SuppressionList()


def _span(text: str, start: int = 0, source: str = "knowledgator") -> dict:
    return {
        "start": start,
        "end": start + len(text),
        "text": text,
        "canonical": "PERSON",
        "raw_label": "person name",
        "source": source,
        "score": 0.9,
    }


# ─── 拼音音节校验 ───


def test_拼音名_通过() -> None:
    for t in ["Shanshan", "Yingying", "Zhao", "Kong", "Lili", "Weiran", "Xiaoming"]:
        assert is_pinyin_name(t), t


def test_非拼音英文词_拒绝() -> None:
    for t in ["Kek", "Hugues", "Obsidian", "Vault", "Please", "mio"]:
        assert not is_pinyin_name(t), t


# ─── gate / recall 判定（用真实生成的名单文件）───


def test_gate_名单名与拼音都认识() -> None:
    gaz = get_gazetteer()
    for t in ["Philip", "Priscilla", "Maggie", "Zhao", "Shanshan"]:
        assert gaz.is_known_person_token(t), t


def test_gate_常用非名词_确定性误报() -> None:
    gaz = get_gazetteer()
    # 注：Figure 不在此列——fi·gu·re 恰可拆为合法拼音音节被放行（宽松组合的
    # 已知边界）；生产中该词以小写出现，由 S2 纯小写规则覆盖
    for t in ["Vault", "Income", "Action", "Please", "Tue"]:
        assert gaz.is_common_nonname_word(t), t


def test_gate_名字与拼音_不算常用非名词() -> None:
    gaz = get_gazetteer()
    # May/Will 是常用词但也是名字；Shanshan 是拼音 → 都不 drop
    for t in ["May", "Will", "Philip", "Shanshan"]:
        assert not gaz.is_common_nonname_word(t), t


def test_gate_未知罕见token_不算常用非名词() -> None:
    gaz = get_gazetteer()
    # 多元文化人名不在任何名单 → 默认保留（public_en recall 崩盘教训）
    for t in ["Anouk", "Hugues", "Kek", "Obsidian", "Yarle"]:
        assert not gaz.is_common_nonname_word(t), t


def test_recall_歧义名被排除() -> None:
    gaz = get_gazetteer()
    # May/Will 是名字也是高频词 → gate 认识、recall 不参与
    for t in ["May", "Will", "June"]:
        assert gaz.is_known_person_token(t), t
        assert not gaz.recall_eligible(t), t
    assert gaz.recall_eligible("Priscilla")


# ─── GazetteerDetector 补召回 ───


@pytest.fixture
def detector(canonical_map):
    return GazetteerDetector(canonical_map)


async def test_recall_补漏单名(detector) -> None:
    spans = await detector.detect("I spoke with Priscilla about the fund.")
    assert [s["text"] for s in spans] == ["Priscilla"]
    assert spans[0]["canonical"] == "PERSON"
    assert spans[0]["source"] == "gazetteer"


async def test_recall_FirstLast缝合为一个span(detector) -> None:
    # Kowalski：census 姓氏名单内、非歧义（Parker 类高频词姓氏会被歧义表排除）
    spans = await detector.detect("Meeting with Ethan Kowalski tomorrow.")
    assert [s["text"] for s in spans] == ["Ethan Kowalski"]


async def test_recall_歧义名不出span(detector) -> None:
    # May/Will 高频词歧义 → 不补召回（NER 认了才算）
    spans = await detector.detect("May I ask? Will you go?")
    assert spans == []


async def test_recall_小写与全大写不出span(detector) -> None:
    spans = await detector.detect("please send ETHAN the summary")
    assert spans == []


async def test_recall_普通句子零误报(detector) -> None:
    spans = await detector.detect("The quarterly report shows revenue growth.")
    assert spans == []


# ─── precision S7 gate 端到端 ───


def test_S7_常用非名词单名_drop() -> None:
    text = "Store keys in the Vault module"
    out = apply_precision_rules([_span("Vault", start=18)], text, _EMPTY)
    assert out == []


def test_S7_名单单名_保留() -> None:
    text = "Ask Priscilla for details"
    out = apply_precision_rules([_span("Priscilla", start=4)], text, _EMPTY)
    assert len(out) == 1


def test_S7_拼音单名_保留() -> None:
    text = "同事 Shanshan 会跟进"
    out = apply_precision_rules([_span("Shanshan", start=3)], text, _EMPTY)
    assert len(out) == 1


def test_S7_未知罕见名_默认保留() -> None:
    # 多元文化人名（不在 SSA/Census 名单）默认保留——宁多脱敏不漏
    text = "Please contact Hugues and Anouk tomorrow"
    out = apply_precision_rules(
        [
            _span("Hugues", start=text.index("Hugues")),
            _span("Anouk", start=text.index("Anouk")),
        ],
        text, _EMPTY,
    )
    assert len(out) == 2


def test_S7_产品名走suppression兜底() -> None:
    # Obsidian 非常用词（gate 不拦），由 suppression overlay 兜底 drop
    from pathlib import Path
    sup = SuppressionList.from_yaml(
        Path(__file__).resolve().parent.parent / "config" / "suppression-list.yaml"
    )
    text = "Use Obsidian for notes"
    out = apply_precision_rules([_span("Obsidian", start=4)], text, sup)
    assert out == []


def test_S7_多词全名_不受gate影响() -> None:
    # 多词名走 NER 判断，gate 只管单 token（Yuhui Zuo 不在名单也保留）
    text = "Yuhui Zuo joined the call"
    out = apply_precision_rules([_span("Yuhui Zuo")], text, _EMPTY)
    assert len(out) == 1


def test_S7_gazetteer自产span_gate放行() -> None:
    # 补召回 span 的 token 天然在名单内，不会被自己 gate 掉
    text = "I spoke with Priscilla today"
    span = _span("Priscilla", start=13, source="gazetteer")
    span["raw_label"] = "GAZETTEER_PERSON"
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1


# ─── 名单文件缺失退化 ───


def test_名单缺失_gate全放行不误杀(tmp_path) -> None:
    gaz = Gazetteer.from_dir(tmp_path)  # 空目录 → 空集
    # 常用词表缺失 → is_common_nonname_word 恒 False → 全放行（fail-safe 不误杀）
    assert not gaz.is_common_nonname_word("Vault")
    assert gaz.is_known_person_token("Shanshan")  # 拼音兜底仍生效
