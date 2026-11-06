"""按 level 跑识别器并判定降级（ADR-003 §2）。

level：full = 全部识别器；regex = 只跑不加载模型的识别器（heavy=False）；none = 不识别。
一个请求的多段文本并发识别，但整个请求只占一个排队名额。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from ..merge import apply_context_rules, merge_spans
from ..parallel import InferenceLimiter, Overloaded, run_all
from .errors import ApiError

logger = logging.getLogger(__name__)

# 一个请求同时在推理的文本段上限：多叶子的 json 请求不能占满全部推理槽位，饿死其他请求
_SEGMENTS_IN_FLIGHT = 2


@dataclass
class Detection:
    spans: list[list[dict[str, Any]]]  # 与输入文本一一对应，已 merge + 上下文规则
    applied_level: str
    degrade_reason: str | None = None
    detectors_failed: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.degrade_reason is not None


async def detect_texts(
    texts: list[str],
    *,
    level: str,
    allow_degrade: bool,
    detectors: list,
    limiter: InferenceLimiter | None,
    timeout_sec: float,
    serial: bool = False,
    segments_in_flight: int = _SEGMENTS_IN_FLIGHT,
) -> Detection:
    if level == "none" or not texts:
        return Detection([[] for _ in texts], level)
    light = [d for d in detectors if not d.heavy]
    heavy_names = {d.name for d in detectors if d.heavy}
    use = light if level == "regex" else detectors
    reason: str | None = None
    admitted = False
    if level == "full" and heavy_names:
        if any(not d.ready for d in detectors if d.heavy):
            reason = "not_ready"
        elif limiter is not None:
            try:
                limiter.enter()
                admitted = True
            except Overloaded:
                reason = "overloaded"
        if reason is not None:
            if not allow_degrade:
                raise ApiError(reason, "overloaded, retry later" if reason == "overloaded" else "models not ready")
            use = light
    in_flight = asyncio.Semaphore(segments_in_flight)

    async def one(text: str):
        async with in_flight:
            return await run_all(use, text, timeout_sec=timeout_sec, serial=serial, limiter=limiter, admit=False)

    try:
        results = await asyncio.gather(*(one(t) for t in texts))
    finally:
        if admitted:
            limiter.leave()

    failed = sorted({n for r in results for n in r.failed})
    if any(n not in heavy_names for n in failed):
        raise ApiError("detect_failed", f"regex detectors failed: {failed}")
    if failed and reason is None:
        if not allow_degrade:
            raise ApiError("detect_failed", f"detectors failed: {failed}")
        timed_out = any(r.timed_out for r in results)
        reason = "detector_timeout" if timed_out else "detector_error"
    try:
        spans = [apply_context_rules(merge_spans(r.spans), t) for r, t in zip(results, texts)]
    except Exception as exc:  # noqa: BLE001  规则配置坏了：连正则结果也给不出
        logger.exception("merge / context rules failed")
        raise ApiError("detect_failed", f"post-processing failed: {exc}") from exc
    if reason is not None:
        return Detection(spans, "regex", reason, failed)
    return Detection(spans, level, None, failed)
