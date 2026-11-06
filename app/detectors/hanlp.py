"""HanLP MTL ELECTRA Base 识别器。

- 仅在文本含 ≥ HANLP_MIN_ZH_CHARS 汉字时启用
- token → char 偏移转换（HanLP 的 NER 返回 token 索引）
- canonical.yaml:hanlp 中未列出的 raw_label 全部 drop
- 角色词黑名单（甲方/乙方 等）
- port 自 privacy-filter-eval/reports/nvda_case/run_pipeline.py:run_hanlp
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..chunking import chunk_with_offsets, count_zh_chars
from ..settings import settings
from .base import ChunkFailures, Detector

logger = logging.getLogger(__name__)

_MODEL = None  # 进程级单例


def _load_model():
    global _MODEL
    if _MODEL is None:
        import hanlp

        logger.info("loading HanLP MTL ELECTRA base ...")
        _MODEL = hanlp.load(
            hanlp.pretrained.mtl.CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_BASE_ZH
        )
        logger.info("HanLP loaded")
    return _MODEL


class HanlpDetector(Detector):
    name = "hanlp"
    heavy = True

    def __init__(self, canonical_map):
        super().__init__(canonical_map)
        self._loaded = False

    def warmup(self) -> None:
        _load_model()
        self._loaded = True

    @property
    def ready(self) -> bool:
        return self._loaded

    def skips(self, text: str) -> bool:
        return count_zh_chars(text) < settings.hanlp_min_zh_chars

    async def detect(self, text: str) -> list[dict[str, Any]]:
        if self.skips(text):
            return []
        # 模型推理是 CPU bound，跑在线程池里不阻塞 event loop
        return await asyncio.to_thread(self._detect_sync, text)

    def _detect_sync(self, text: str) -> list[dict[str, Any]]:
        model = _load_model()
        spans: list[dict[str, Any]] = []
        chunks = chunk_with_offsets(text, settings.hanlp_chunk_chars, overlap=0)
        blacklist = self.canonical_map.hanlp_role_blacklist
        failed = 0

        for ck in chunks:
            try:
                result = model(ck.text, tasks=["ner/ontonotes"])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"HanLP chunk @{ck.global_start} 失败: {exc}")
                failed += 1
                continue
            ner_list = result.get("ner/ontonotes", []) if hasattr(result, "get") else []
            tokens = result.get("tok/fine") or result.get("tok/coarse") or []

            # token → char offsets within chunk
            char_offsets: list[tuple[int, int]] = []
            cursor = 0
            for tok in tokens:
                idx = ck.text.find(tok, cursor)
                if idx >= 0:
                    char_offsets.append((idx, idx + len(tok)))
                    cursor = idx + len(tok)
                else:
                    char_offsets.append((cursor, cursor + len(tok)))
                    cursor += len(tok)

            for ent in ner_list:
                if len(ent) < 4:
                    continue
                ent_text, raw_label, tok_start, tok_end = ent[0], ent[1], ent[2], ent[3]
                canonical = self.canonical_map.lookup("hanlp", raw_label)
                if canonical is None:
                    continue  # drop（NORP / GPE / DATE / MONEY 等都在此 drop）
                if ent_text in blacklist:
                    continue
                if tok_start < len(char_offsets) and 0 < tok_end <= len(char_offsets):
                    local_start = char_offsets[tok_start][0]
                    local_end = char_offsets[tok_end - 1][1]
                else:
                    local_start = ck.text.find(ent_text)
                    if local_start < 0:
                        continue
                    local_end = local_start + len(ent_text)
                spans.append({
                    "source": "hanlp",
                    "raw_label": raw_label,
                    "canonical": canonical,
                    "start": ck.global_start + local_start,
                    "end": ck.global_start + local_end,
                    "text": ck.text[local_start:local_end],
                    "score": 0.85,
                })
        if failed:
            raise ChunkFailures(failed, len(chunks), spans)
        return spans
