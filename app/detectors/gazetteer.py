"""S7 (round11) recall 方向：gazetteer 人名补召回 detector。

背景：round9 评测 public_en PERSON recall 仅 61%（NER 漏识别英文名）。
本 detector 用名单（given ∪ surname，剔除歧义名）扫描 proper-case token，
补上 NER 漏掉的人名 span：

- 仅匹配首字母大写、其余小写的 token（May/WILL 全大写不算）；
- 歧义名（May/Will/June 等名字∩英文高频词）不参与补召回（gazetteer.recall_eligible）；
- 相邻命中 token（单空格间隔）缝合为一个 span（First Last → 一个占位符）；
- 优先级 gazetteer=1（app/merge.py PRIORITY），与 NER 重叠时 NER 胜出，
  纯补漏不抢已有结果。
"""
from __future__ import annotations

import re
from typing import Any

from ..gazetteer import get_gazetteer
from .base import Detector

_PROPER_TOKEN = re.compile(r"\b[A-Z][a-z]{1,19}\b")
_RAW_LABEL = "GAZETTEER_PERSON"
_SCORE = 0.55


class GazetteerDetector(Detector):
    name = "gazetteer"

    async def detect(self, text: str) -> list[dict[str, Any]]:
        canonical = self.canonical_map.lookup("gazetteer", _RAW_LABEL)
        if canonical is None:
            return []
        gaz = get_gazetteer()
        hits = [
            m for m in _PROPER_TOKEN.finditer(text)
            if gaz.recall_eligible(m.group(0))
        ]
        if not hits:
            return []
        # 相邻命中缝合（gap 为单个空格 → First Last 合成一个 span）
        spans: list[dict[str, Any]] = []
        cur_start, cur_end = hits[0].start(), hits[0].end()
        for m in hits[1:]:
            if m.start() == cur_end + 1 and text[cur_end] == " ":
                cur_end = m.end()
                continue
            spans.append(self._span(text, cur_start, cur_end, canonical))
            cur_start, cur_end = m.start(), m.end()
        spans.append(self._span(text, cur_start, cur_end, canonical))
        return spans

    def _span(
        self, text: str, start: int, end: int, canonical: str
    ) -> dict[str, Any]:
        return {
            "source": self.name,
            "raw_label": _RAW_LABEL,
            "canonical": canonical,
            "start": start,
            "end": end,
            "text": text[start:end],
            "score": _SCORE,
        }
