"""Microsoft Presidio 检测器（Round 9 · 非 GLiNER 轻量全栈方案）。

Presidio = spaCy NER + 一组内置正则识别器（email/phone/credit_card/iban/crypto/ssn…）。
本类把它当作"英文全栈引擎"：既出 PERSON/ORG/ADDRESS（NER），也出结构化 PII（正则识别器），
统一映射回本服务 13 类 canonical。CPU 上靠 spaCy 统计模型，比 GLiNER 快一个数量级。

关键修正（上一轮 Presidio ORG 召回为 0 的根因）：Presidio 默认的 spaCy→Presidio 实体映射
**不含 ORG**，必须在 `ner_model_configuration.model_to_presidio_entity_mapping` 里显式加
`ORG→ORGANIZATION`，否则组织名全漏。

- Presidio entity_type → canonical：见 `_ENTITY_MAP`；未映射的（URL/DATE_TIME/NRP/US_DRIVER_LICENSE…）drop。
- score 阈值过滤（默认 0.35，丢掉 spaCy 0.01 的 driver_license 噪声，保留 0.40 的 phone/bank_number）。
- 文档级门控 + span 级 CJK-drop：与 SpacyDetector 同口径（避免在纯中文文档上空跑/误报）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import Detector
from .nvidia import _CJK_DROP_RATIO, _cjk_ratio
from .spacy_ner import _MIN_ASCII_LETTERS, _ascii_letter_count, _has_ascii_upper

logger = logging.getLogger(__name__)

# proper-case 护栏只作用于 NER 派生类（人名/机构/地名），结构化类（邮箱/电话/卡号…）不受限
_NER_CANONICALS = {"PERSON", "ORG", "ADDRESS"}

# Presidio entity_type → 本服务 13 canonical（未列出 = drop）
_ENTITY_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "ORGANIZATION": "ORG",
    "LOCATION": "ADDRESS",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "BANK_CARD",
    "US_BANK_NUMBER": "ACCOUNT",
    "IBAN_CODE": "ACCOUNT",
    "CRYPTO": "CRYPTO_ADDR",
    "US_SSN": "ID_CARD",
    "US_ITIN": "TIN",
    "US_PASSPORT": "ID_CARD",
}

# spaCy 模型 label → Presidio entity（含 ORG，修正默认映射缺失）
_NER_MAPPING: dict[str, str] = {
    "PERSON": "PERSON", "PER": "PERSON",
    "GPE": "LOCATION", "LOC": "LOCATION", "FAC": "LOCATION",
    "ORG": "ORGANIZATION", "NORP": "NRP",
}
_NER_LABELS_TO_IGNORE = [
    "CARDINAL", "ORDINAL", "DATE", "TIME", "MONEY", "PERCENT",
    "QUANTITY", "EVENT", "WORK_OF_ART", "LAW", "LANGUAGE", "PRODUCT",
]

_ENGINES: dict[str, Any] = {}  # spacy_model → AnalyzerEngine（进程级单例）


def _build_engine(spacy_model: str):
    eng = _ENGINES.get(spacy_model)
    if eng is None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        logger.info(f"building Presidio AnalyzerEngine (spaCy {spacy_model}) ...")
        cfg = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": spacy_model}],
            "ner_model_configuration": {
                "model_to_presidio_entity_mapping": _NER_MAPPING,
                "low_confidence_score_multiplier": 0.4,
                "low_score_entity_names": [],
                "labels_to_ignore": _NER_LABELS_TO_IGNORE,
            },
        }
        nlp_engine = NlpEngineProvider(nlp_configuration=cfg).create_engine()
        eng = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
        _ENGINES[spacy_model] = eng
        logger.info("Presidio AnalyzerEngine ready")
    return eng


class PresidioDetector(Detector):
    """Presidio 英文全栈检测器（NER + 内置正则识别器）。"""
    heavy = True

    def __init__(self, canonical_map, *, name: str = "presidio",
                 spacy_model: str = "en_core_web_lg", score_threshold: float = 0.35,
                 require_proper_case: bool = False):
        super().__init__(canonical_map)
        self.name = name
        self.spacy_model = spacy_model
        self.score_threshold = score_threshold
        # proper-case 护栏：True 时对 NER 类（PERSON/ORG/ADDRESS）要求含大写字母
        self.require_proper_case = require_proper_case
        self._loaded = False

    def warmup(self) -> None:
        _build_engine(self.spacy_model)
        self._loaded = True

    @property
    def ready(self) -> bool:
        return self._loaded

    async def detect(self, text: str) -> list[dict[str, Any]]:
        if _ascii_letter_count(text) < _MIN_ASCII_LETTERS:
            return []
        return await asyncio.to_thread(self._detect_sync, text)

    def _detect_sync(self, text: str) -> list[dict[str, Any]]:
        engine = _build_engine(self.spacy_model)
        spans: list[dict[str, Any]] = []
        results = engine.analyze(text=text, language="en")
        for r in results:
            canonical = _ENTITY_MAP.get(r.entity_type)
            if canonical is None:
                continue
            if float(r.score) < self.score_threshold:
                continue
            ent_text = text[r.start:r.end]
            if len(ent_text.strip()) < 2:
                continue
            if _cjk_ratio(ent_text) >= _CJK_DROP_RATIO:
                continue
            if (self.require_proper_case and canonical in _NER_CANONICALS
                    and not _has_ascii_upper(ent_text)):
                continue  # NER 类小写误标 → drop（结构化类不受限）
            spans.append({
                "source": self.name,
                "raw_label": r.entity_type.lower(),
                "canonical": canonical,
                "start": r.start,
                "end": r.end,
                "text": ent_text,
                "score": float(r.score),
            })
        return spans
