"""NVIDIA gliner-PII 识别器。

- zero-shot，传入 canonical_map.nvidia_labels（精简到 10 个家办高频 label）
- chunk + global offset 还原
- MPS/CUDA/CPU 自适应
- port 自 privacy-filter-eval/reports/nvda_case/run_pipeline.py:run_nvidia
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..chunking import chunk_with_offsets
from ..settings import settings
from .base import ChunkFailures, Detector

logger = logging.getLogger(__name__)

_MODEL = None
_DEVICE = "cpu"

# NER 边界噪声后处理：
# gliner-PII 在中文短文本里常吞前后修饰语（如「李明手机号是多少」整句判 PERSON）。
# 思路：剪掉**已知头/尾噪声词**，保留中间真实实体；若剪完是问句残骸/为空 → drop。
# 字典按长度倒序写：长 phrase 先匹配，避免「手机号」抢「手机号是多少」。
_TAIL_TRIM_TOKENS = (
    "手机号码是多少",
    "电话号码是多少",
    "的手机号码是多少",
    "的电话号码是多少",
    "的手机号是多少",
    "的电话是多少",
    "手机号是多少",
    "电话是多少",
    "邮箱是多少",
    "的手机号码",
    "的电话号码",
    "的手机号",
    "的电话",
    "的邮箱",
    "的地址",
    "的名字",
    "手机号码",
    "电话号码",
    "手机号",
    "邮箱",
    "地址",
    "是多少",
    "是什么",
    "是谁",
    "怎么样",
    "很厉害",
    "办公",
    "?",
    "？",
)
_HEAD_TRIM_TOKENS = (
    "我的同事",
    "我朋友",
    "我同事",
    "这位",
    "客户",
    "他叫",
    "她叫",
)
# trim 后若 span 仍含这些标记（说明本身就是问句残骸） → drop
_QUESTION_LEFTOVER_MARKERS = (
    "什么", "谁", "吗", "呢", "哪", "多少", "怎么", "几", "为何",
    "啥", "如何", "为什么", "是不是",
)

# 中文场景让 HanLP 独占 PERSON/ADDRESS/ORG：
# gliner-PII 是多语言 zero-shot，中文短句上 FP 极高（"的电话我写错了"/"刚才说的客户"
# 这类动词短语字典穷举不完）。当 span 文本中 CJK 字符占比 ≥ 阈值时整段 drop，
# 让 HanLP 独自处理中文实体；nvidia 仍负责英文/数字主导的 span（ORG / ADDRESS 跨语言）。
_CJK_DROP_RATIO = 0.5


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return cjk / len(text)


def _trim_noise(text: str) -> tuple[int, int]:
    """剪头尾噪声词。返回相对 text 的 (start, end) 偏移；trim 后为空时返回 (0, 0)"""
    start, end = 0, len(text)
    # 先尾后头，循环直到没法再剪（处理「我朋友Tom很厉害」类双头噪声）
    changed = True
    while changed:
        changed = False
        sub = text[start:end]
        for tail in _TAIL_TRIM_TOKENS:
            if sub.endswith(tail) and len(sub) > len(tail):
                end -= len(tail)
                changed = True
                break
        sub = text[start:end]
        for head in _HEAD_TRIM_TOKENS:
            if sub.startswith(head) and len(sub) > len(head):
                start += len(head)
                changed = True
                break
    return start, end


def _pick_device(prefer: str = "auto") -> str:
    if prefer != "auto":
        return prefer
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def _load_model():
    global _MODEL, _DEVICE
    if _MODEL is None:
        from gliner import GLiNER

        _DEVICE = _pick_device(settings.gpu_device)
        logger.info(f"loading NVIDIA gliner-PII on {_DEVICE} ...")
        model_path = settings.nvidia_model_path or "nvidia/gliner-PII"
        local_only = bool(settings.nvidia_model_path)
        _MODEL = GLiNER.from_pretrained(model_path, local_files_only=local_only)
        try:
            _MODEL = _MODEL.to(_DEVICE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f".to({_DEVICE}) failed: {exc}; fallback cpu")
            _DEVICE = "cpu"
        logger.info(f"NVIDIA loaded on {_DEVICE}")
    return _MODEL


class NvidiaDetector(Detector):
    heavy = True
    name = "nvidia"

    def __init__(self, canonical_map):
        super().__init__(canonical_map)
        self._loaded = False

    def warmup(self) -> None:
        _load_model()
        self._loaded = True

    @property
    def ready(self) -> bool:
        return self._loaded

    async def detect(self, text: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._detect_sync, text)

    def _detect_sync(self, text: str) -> list[dict[str, Any]]:
        model = _load_model()
        labels = self.canonical_map.nvidia_labels
        spans: list[dict[str, Any]] = []
        chunks = chunk_with_offsets(
            text,
            settings.nvidia_chunk_chars,
            overlap=settings.nvidia_chunk_overlap,
        )
        failed = 0
        for ck in chunks:
            try:
                raw = model.predict_entities(
                    ck.text, labels, threshold=settings.nvidia_threshold
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"NVIDIA chunk @{ck.global_start} 失败: {exc}")
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
                # 头尾噪声 trim：把「李明手机号是多少」剪成「李明」、「我朋友Tom很厉害」剪成「Tom」
                trim_start, trim_end = _trim_noise(raw_text)
                trimmed = raw_text[trim_start:trim_end]
                # trim 后仍含问句词 → 整段是问句残骸（如「客户是谁」），drop
                if any(m in trimmed for m in _QUESTION_LEFTOVER_MARKERS):
                    logger.debug(
                        f"NVIDIA drop residual question: {canonical} {raw_text!r}"
                    )
                    continue
                # 太短（trim 后只剩 1 char 或为空）→ 不可信 drop
                if len(trimmed.strip()) < 2:
                    logger.debug(
                        f"NVIDIA drop too short after trim: {canonical} {raw_text!r}"
                    )
                    continue
                # 中文 span 整段 drop：让 HanLP 独占 PERSON/ADDRESS/ORG
                if _cjk_ratio(trimmed) >= _CJK_DROP_RATIO:
                    logger.debug(
                        f"NVIDIA drop CJK-dominant span: {canonical} {raw_text!r}"
                    )
                    continue
                final_gs = gs + trim_start
                final_ge = gs + trim_end
                spans.append({
                    "source": "nvidia",
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
