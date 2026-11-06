"""WS-0.1 可配置 detector profile / registry 单测。

只覆盖正则 detector（cn_regex / en_regex），不加载 torch / hanlp / gliner，
保证默认 pytest（非 slow）可快速跑通。模型类组合的实测放在 slow / api 测试与
Round 9 评测脚本里。
"""
from __future__ import annotations

import pytest

from app.canonical import CanonicalMap
from app.detectors import DETECTOR_REGISTRY, build_detectors, parse_detector_names
from app.settings import settings


@pytest.fixture
def canon() -> CanonicalMap:
    return CanonicalMap(settings.config_dir / "canonical.yaml")


def test_parse_detector_names_basic() -> None:
    assert parse_detector_names("cn_regex,en_regex,hanlp,nvidia") == [
        "cn_regex",
        "en_regex",
        "hanlp",
        "nvidia",
    ]


def test_parse_detector_names_strips_and_dedups() -> None:
    # 去空白、去空项、保持顺序去重
    assert parse_detector_names(" cn_regex , en_regex ,cn_regex,, ") == [
        "cn_regex",
        "en_regex",
    ]


def test_parse_detector_names_empty() -> None:
    assert parse_detector_names("") == []
    assert parse_detector_names("   ") == []


def test_registry_has_base_detectors() -> None:
    for name in ("cn_regex", "en_regex", "hanlp", "nvidia"):
        assert name in DETECTOR_REGISTRY


def test_registry_has_round9_candidates() -> None:
    # Round 9 新增的 GLiNER 候选（drop-in 替换 nvidia 角色）
    assert "urchade" in DETECTOR_REGISTRY
    assert "knowledgator" in DETECTOR_REGISTRY


def test_registry_has_nlp_candidates() -> None:
    # Round 9 非 GLiNER 轻量候选（spaCy / Presidio，英文 NER 角色）
    assert "spacy" in DETECTOR_REGISTRY
    assert "presidio" in DETECTOR_REGISTRY


def test_spacy_label_map_and_gate() -> None:
    # 不加载 spaCy 模型，只校验纯函数：label 映射 + ASCII 门控
    from app.detectors.spacy_ner import _LABEL_MAP, _ascii_letter_count

    assert _LABEL_MAP["PERSON"] == "PERSON"
    assert _LABEL_MAP["ORG"] == "ORG"
    assert _LABEL_MAP["GPE"] == "ADDRESS"
    assert _LABEL_MAP["LOC"] == "ADDRESS"
    assert "DATE" not in _LABEL_MAP  # 时间/数量类不计入 NER 角色
    assert _ascii_letter_count("纯中文没有字母。") == 0
    assert _ascii_letter_count("John 给 张三 转账") == 4


def test_proper_case_guard() -> None:
    # proper-case 护栏：英文专有名词含大写 → 保留；纯小写 → drop
    from app.detectors.spacy_ner import _has_ascii_upper

    assert _has_ascii_upper("Goldman Sachs") is True
    assert _has_ascii_upper("HSBC") is True
    assert _has_ascii_upper("New York") is True
    assert _has_ascii_upper("apple") is False  # 小写常用词误标 → 会被砍
    assert _has_ascii_upper("发改委") is False


def test_registry_has_tuning_profiles() -> None:
    # Round 9 调优 profile：sm 更快 / proper-case 护栏变体 / 砍标签变体
    for name in ("spacy_sm", "spacy_pc", "presidio_pc", "spacy_person", "spacy_pa"):
        assert name in DETECTOR_REGISTRY


def test_spacy_allowed_canonicals(canon: CanonicalMap) -> None:
    # 砍标签：spacy_person 只留 PERSON，spacy_pa 留 PERSON+ADDRESS（不加载模型）
    from app.detectors import _build_spacy_pa, _build_spacy_person

    person = _build_spacy_person(canon, settings)
    assert person.allowed_canonicals == {"PERSON"}
    pa = _build_spacy_pa(canon, settings)
    assert pa.allowed_canonicals == {"PERSON", "ADDRESS"}


def test_presidio_entity_map() -> None:
    # 不构建 AnalyzerEngine，只校验 entity_type → canonical 映射 + ORG 修正存在
    from app.detectors.presidio_det import _ENTITY_MAP, _NER_MAPPING

    assert _ENTITY_MAP["ORGANIZATION"] == "ORG"
    assert _ENTITY_MAP["LOCATION"] == "ADDRESS"
    assert _ENTITY_MAP["EMAIL_ADDRESS"] == "EMAIL"
    assert _ENTITY_MAP["CREDIT_CARD"] == "BANK_CARD"
    assert "URL" not in _ENTITY_MAP  # URL 不在 13 canonical，drop
    # 修正默认映射缺失 ORG（上一轮 Presidio ORG 召回为 0 的根因）
    assert _NER_MAPPING["ORG"] == "ORGANIZATION"


def test_span_accepts_pluggable_source() -> None:
    # 回归：Span.source 不能是封闭 Literal，否则新候选 detector 的 span 触发 500
    from app.schemas import Span

    s = Span(start=0, end=3, text="abc", canonical="PERSON",
             raw_label="person name", source="urchade", score=0.9)
    assert s.source == "urchade"


def test_build_regex_detectors_preserves_order(canon: CanonicalMap) -> None:
    det = build_detectors(["en_regex", "cn_regex"], canon, settings, warmup=False)
    assert list(det.keys()) == ["en_regex", "cn_regex"]
    assert det["cn_regex"].name == "cn_regex"
    assert det["en_regex"].name == "en_regex"


def test_build_unknown_detector_raises(canon: CanonicalMap) -> None:
    with pytest.raises(ValueError):
        build_detectors(["cn_regex", "does_not_exist"], canon, settings, warmup=False)


def test_settings_detector_names_default() -> None:
    # 默认 profile 应至少含两个正则 detector，且解析为有序列表
    names = settings.detector_names
    assert isinstance(names, list)
    assert "cn_regex" in names and "en_regex" in names


def test_default_profile_uses_knowledgator_not_nvidia() -> None:
    # Round 9 定稿（D9）：默认英文/多语 NER 主力 nvidia → knowledgator-base。
    # 锁定决策——默认 profile 必须含 knowledgator 且不含 nvidia。
    # Round 11（S7）：默认组合追加 gazetteer 人名补召回。
    names = settings.detector_names
    assert "knowledgator" in names
    assert "nvidia" not in names
    assert names == ["cn_regex", "en_regex", "knowledgator", "gazetteer"]


def test_default_profile_excludes_hanlp() -> None:
    # LOOK-5231：HanLP 模型是 CC BY-NC-SA 4.0（非商用），对外版本默认不能带。
    # 内部部署在 docker-compose 里显式开启 hanlp，不依赖这个默认值。
    assert "hanlp" not in settings.detector_names


def test_gliner_threshold_default_is_sweet_spot() -> None:
    # knowledgator @0.5 是 FP-neg 甜点（Round 9）；与 nvidia_threshold(0.4) 分开。
    assert settings.gliner_threshold == 0.5
    assert settings.nvidia_threshold == 0.4


def test_gliner_detector_threshold_param(canon: CanonicalMap) -> None:
    # GlinerDetector 阈值：默认 None（运行时回退 settings.gliner_threshold），可显式覆盖。
    # 不加载模型（仅校验构造期属性）。
    from app.detectors import _build_knowledgator
    from app.detectors.gliner_generic import GlinerDetector

    kg = _build_knowledgator(canon, settings)
    assert kg.threshold is None  # 工厂不写死，运行时用 settings.gliner_threshold
    assert kg.onnx_file == "onnx/model_quint8.onnx"  # 走 ONNX-INT8 最小集

    explicit = GlinerDetector(canon, name="kg", model_repo="x/y", threshold=0.6)
    assert explicit.threshold == 0.6


def test_gliner_chunk_chars_is_independent_knob() -> None:
    # GLiNER 候选有独立的 chunk 旋钮（与 nvidia_chunk_chars 解耦，可单独调）。
    # 默认 800：实测提到 1800 对延迟/召回无收益（见 settings 注释），故维持 800。
    assert settings.gliner_chunk_chars == 800
    assert settings.gliner_chunk_overlap == 80
    assert settings.nvidia_chunk_chars == 800
    # 是两个独立字段（同名不同源），改其一不应牵连另一个。
    assert "gliner_chunk_chars" in type(settings).model_fields
    assert "nvidia_chunk_chars" in type(settings).model_fields


def test_gliner_detector_chunks_with_gliner_chunk_chars(canon, monkeypatch) -> None:
    # 行为断言：GlinerDetector 切 chunk 用的是 settings.gliner_chunk_chars，不是 nvidia_chunk_chars。
    # 不加载真实模型：stub _load 返回假模型（无命中），并捕获传给 chunk_with_offsets 的 chunk_size。
    from app.detectors import gliner_generic
    from app.detectors.gliner_generic import GlinerDetector

    captured: dict = {}
    real_chunk = gliner_generic.chunk_with_offsets

    def spy(text, chunk_size, overlap=0):
        captured["chunk_size"] = chunk_size
        captured["overlap"] = overlap
        return real_chunk(text, chunk_size, overlap=overlap)

    monkeypatch.setattr(gliner_generic, "chunk_with_offsets", spy)

    det = GlinerDetector(canon, name="kg", model_repo="x/y")

    class _FakeModel:
        def predict_entities(self, text, labels, threshold):
            return []  # 不命中，专注校验 chunk 参数

    monkeypatch.setattr(det, "_load", lambda: _FakeModel())

    det._detect_sync("长" * 4000)  # 足够长以触发多 chunk
    assert captured["chunk_size"] == settings.gliner_chunk_chars == 800
    assert captured["overlap"] == settings.gliner_chunk_overlap == 80
    # 关键：用的是 gliner_chunk_chars 而非 nvidia_chunk_chars（即便当前两者同值，来源不同）。
