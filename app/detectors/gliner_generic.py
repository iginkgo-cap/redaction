"""通用 GLiNER 检测器（Round 9）。

承担与 NvidiaDetector 完全相同的"英文/多语 NER 角色"（PERSON/ADDRESS/ORG），
但底层模型可换（urchade / knowledgator / vicgalle 等标准 GLiNER）。复用 nvidia.py 的
chunk + 噪声 trim + CJK-drop 逻辑，以及 canonical.yaml 的 `nvidia:` label 配置，
使其成为 NVIDIA 的 **drop-in 替换**，便于 Round 9 组合级 A/B（换模型不动 merge/canonical）。

注意：fastino(GLiNER2) 依赖 transformers 4.51 与本服务栈(4.46)冲突，**不能**用本类加载，
需独立 sidecar；本类只服务于标准 GLiNER（gliner v0.2 / transformers 4.46）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..chunking import chunk_with_offsets
from ..settings import settings
from .base import ChunkFailures, Detector
from .nvidia import (
    _CJK_DROP_RATIO,
    _QUESTION_LEFTOVER_MARKERS,
    _cjk_ratio,
    _pick_device,
    _trim_noise,
)

logger = logging.getLogger(__name__)


class GlinerDetector(Detector):
    """可换模型的标准 GLiNER 检测器。

    name 同时作为 detector 名与 span 的 source（merge 优先级按 source 取），
    复用 canonical 'nvidia' 段的 3 个 label 与映射。
    """
    heavy = True

    def __init__(self, canonical_map, *, name: str, model_repo: str,
                 onnx_file: str | None = None, threshold: float | None = None):
        super().__init__(canonical_map)
        self.name = name
        self.model_repo = model_repo
        # 设置 onnx_file（如 "onnx/model_quint8.onnx"）则走 ONNX-INT8 加载，
        # CPU 上比 pytorch 快一个数量级（onnxruntime，无 torch 线程争抢/冷启动问题）。
        self.onnx_file = onnx_file
        # 阈值：None → 用 settings.gliner_threshold（GLiNER 候选共用，knowledgator 甜点 0.5）。
        # 显式传值可按 detector 单独覆盖（A/B 调参用）。
        self.threshold = threshold
        self._model = None
        self._device = "cpu"
        self._loaded = False

    def warmup(self) -> None:
        self._load()
        self._loaded = True

    @property
    def ready(self) -> bool:
        return self._loaded

    def _load(self):
        if self._model is None:
            from gliner import GLiNER

            if self.onnx_file:
                self._device = "cpu"
                logger.info(f"loading GLiNER {self.model_repo} (source={self.name}) "
                            f"ONNX={self.onnx_file} ...")
                self._model = GLiNER.from_pretrained(
                    self.model_repo, load_onnx_model=True,
                    onnx_model_file=self.onnx_file, load_tokenizer=True)
                logger.info(f"GLiNER {self.name} loaded (ONNX, cpu)")
            else:
                self._device = _pick_device(settings.gpu_device)
                logger.info(f"loading GLiNER {self.model_repo} (source={self.name}) on {self._device} ...")
                m = GLiNER.from_pretrained(self.model_repo)
                try:
                    m = m.to(self._device)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f".to({self._device}) failed: {exc}; fallback cpu")
                    self._device = "cpu"
                self._model = m
                logger.info(f"GLiNER {self.name} loaded on {self._device}")
        return self._model

    async def detect(self, text: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._detect_sync, text)

    def _detect_sync(self, text: str) -> list[dict[str, Any]]:
        model = self._load()
        labels = self.canonical_map.nvidia_labels  # 复用 nvidia 的 3 label 配置
        thr = self.threshold if self.threshold is not None else settings.gliner_threshold
        spans: list[dict[str, Any]] = []
        # GLiNER 候选用独立的大 chunk（settings.gliner_chunk_chars，默认 1800），
        # 充分利用其 2048-token 窗口；与 nvidia_chunk_chars(800，为 NVIDIA 384 窗口设) 分离。
        chunks = chunk_with_offsets(
            text, settings.gliner_chunk_chars, overlap=settings.gliner_chunk_overlap
        )
        failed = 0
        for ck in chunks:
            try:
                raw = model.predict_entities(
                    ck.text, labels, threshold=thr
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{self.name} chunk @{ck.global_start} 失败: {exc}")
                failed += 1
                continue
            for r in raw:
                raw_label = str(r["label"]).lower()
                canonical = self.canonical_map.lookup("nvidia", raw_label)
                if canonical is None:
                    continue
                gs = ck.global_start + int(r["start"])
                ge = ck.global_start + int(r["end"])
                raw_text = text[gs:ge]
                trim_start, trim_end = _trim_noise(raw_text)
                trimmed = raw_text[trim_start:trim_end]
                if any(m in trimmed for m in _QUESTION_LEFTOVER_MARKERS):
                    continue
                if len(trimmed.strip()) < 2:
                    continue
                if _cjk_ratio(trimmed) >= _CJK_DROP_RATIO:
                    continue
                final_gs = gs + trim_start
                final_ge = gs + trim_end
                spans.append({
                    "source": self.name,
                    "raw_label": raw_label,
                    "canonical": canonical,
                    "start": final_gs,
                    "end": final_ge,
                    "text": text[final_gs:final_ge],
                    "score": float(r.get("score", 0.5)),
                })
        if failed:
            raise ChunkFailures(failed, len(chunks), spans)
        return spans
