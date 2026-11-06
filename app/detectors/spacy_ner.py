"""spaCy 统计模型 NER 检测器（Round 9 · 非 GLiNER 轻量方案）。

承担与 GLiNER/NVIDIA 相同的"英文 NER 角色"（PERSON/ADDRESS/ORG），但底层是
spaCy 的 CNN/统计模型（`en_core_web_lg` 默认），CPU 上比任何 GLiNER 快一个数量级，
**不依赖 torch GLiNER 冷启动**。与 HanLP（中文 NER）组合即"纯 NLP + 正则、不用大模型"方案。

- 加载时禁用 tagger/parser/lemmatizer/attribute_ruler，只保留 tok2vec+ner（NER 唯一所需），CPU 更快。
- spaCy ent label → canonical：PERSON→PERSON；ORG→ORG；GPE/LOC/FAC→ADDRESS；其余 drop。
- 文档级门控：ASCII 字母过少（纯中文文档）直接跳过，省算力并避免英文模型在中文上误报。
- span 级 CJK-drop：复用 nvidia 的比例阈值，丢掉被切到大量汉字的脏 span（与 GLiNER 口径一致）。
- spaCy 不给 NER 置信度，统一 score=0.85（与 HanLP 同档，merge 优先级按 source 取）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import Detector
from .nvidia import _CJK_DROP_RATIO, _cjk_ratio

logger = logging.getLogger(__name__)

# 仅保留 NER 必需组件，禁用其余以提速（NER 依赖 tok2vec，不依赖 parser/tagger/lemmatizer）。
_DISABLE_PIPES = ["tagger", "parser", "lemmatizer", "attribute_ruler"]

# spaCy OntoNotes ent label → 13 canonical 中承担 NER 角色的 3 类
_LABEL_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "ORG": "ORG",
    "GPE": "ADDRESS",
    "LOC": "ADDRESS",
    "FAC": "ADDRESS",
}

# 运行英文模型的最小 ASCII 字母数（低于此判定为"基本无英文" → 跳过）
_MIN_ASCII_LETTERS = 5

_MODELS: dict[str, Any] = {}  # model_name → nlp（进程级单例，按模型名缓存）


def _ascii_letter_count(text: str) -> int:
    return sum(1 for ch in text if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))


def _has_ascii_upper(text: str) -> bool:
    """英文专有名词（人名/机构/地名）几乎必含大写字母。
    proper-case 护栏：要求 ≥1 个 ASCII 大写字母 → 砍掉 spaCy 把小写常用词误标实体的假阳性。"""
    return any("A" <= ch <= "Z" for ch in text)


def _load_model(model_name: str):
    nlp = _MODELS.get(model_name)
    if nlp is None:
        import spacy

        logger.info(f"loading spaCy {model_name} (disable={_DISABLE_PIPES}) ...")
        nlp = spacy.load(model_name, disable=_DISABLE_PIPES)
        _MODELS[model_name] = nlp
        logger.info(f"spaCy {model_name} loaded; pipes={nlp.pipe_names}")
    return nlp


class SpacyDetector(Detector):
    """spaCy 统计 NER 检测器（可换模型，默认 en_core_web_lg）。"""
    heavy = True

    def __init__(self, canonical_map, *, name: str = "spacy",
                 model_name: str = "en_core_web_lg",
                 require_proper_case: bool = False,
                 allowed_canonicals: set[str] | None = None):
        super().__init__(canonical_map)
        self.name = name
        self.model_name = model_name
        # proper-case 护栏：True 时丢掉不含 ASCII 大写字母的 NER span（降 FP-neg）
        self.require_proper_case = require_proper_case
        # 仅输出这些 canonical（None=全部 PERSON/ORG/ADDRESS）。
        # Round 9 发现 spaCy 的 ORG 精度仅 21%（FP 主凶），只留 PERSON 可大幅降过度脱敏。
        self.allowed_canonicals = allowed_canonicals
        self._loaded = False

    def warmup(self) -> None:
        _load_model(self.model_name)
        self._loaded = True

    @property
    def ready(self) -> bool:
        return self._loaded

    async def detect(self, text: str) -> list[dict[str, Any]]:
        if _ascii_letter_count(text) < _MIN_ASCII_LETTERS:
            return []  # 纯中文文档：英文 NER 无意义，交给 HanLP
        return await asyncio.to_thread(self._detect_sync, text)

    def _detect_sync(self, text: str) -> list[dict[str, Any]]:
        nlp = _load_model(self.model_name)
        spans: list[dict[str, Any]] = []
        doc = nlp(text)
        for ent in doc.ents:
            canonical = _LABEL_MAP.get(ent.label_)
            if canonical is None:
                continue
            if self.allowed_canonicals is not None and canonical not in self.allowed_canonicals:
                continue  # 砍标签：只保留指定类（如只留 PERSON）
            ent_text = text[ent.start_char:ent.end_char]
            if len(ent_text.strip()) < 2:
                continue
            if _cjk_ratio(ent_text) >= _CJK_DROP_RATIO:
                continue  # 切到大量汉字的脏 span（与 GLiNER 口径一致）
            if self.require_proper_case and not _has_ascii_upper(ent_text):
                continue  # 小写常用词误标 → drop（提精度）
            spans.append({
                "source": self.name,
                "raw_label": ent.label_.lower(),
                "canonical": canonical,
                "start": ent.start_char,
                "end": ent.end_char,
                "text": ent_text,
                "score": 0.85,
            })
        return spans
