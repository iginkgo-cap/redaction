"""并行编排：asyncio.gather 跑全部 detector，单 detector 超时不影响整体。

推理准入（2026-10-19 OOM 修复）：模型 detector 经 InferenceLimiter 限并发；排队请求
超过上限抛 Overloaded（API 层转 503）。等槽位期间超时的请求不会启动推理，
避免 wait_for 放弃等待后线程里的推理仍继续跑（僵尸工作）把 CPU/内存拖垮。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .detectors.base import ChunkFailures, Detector

logger = logging.getLogger(__name__)


class Overloaded(Exception):
    """排队请求数已达上限。"""


class InferenceLimiter:
    def __init__(self, max_concurrent: int, max_pending: int):
        self.max_pending = max_pending
        self.pending = 0  # 正在 run_all 里的请求数（含排队）
        self._sem = asyncio.Semaphore(max_concurrent)

    def enter(self) -> None:
        if self.pending >= self.max_pending:
            raise Overloaded(f"pending={self.pending} >= {self.max_pending}")
        self.pending += 1

    def leave(self) -> None:
        self.pending -= 1

    async def run(self, fn, *args):
        # 槽位跟着推理任务走，而不是跟着等待方走：wait_for 超时取消的是等待方，
        # to_thread 里的线程仍在跑，此时还槽位会让真实并发超过上限。
        await self._sem.acquire()
        task = asyncio.ensure_future(fn(*args))
        task.add_done_callback(self._release)
        return await asyncio.shield(task)

    def _release(self, task: asyncio.Future) -> None:
        self._sem.release()
        if not task.cancelled():
            task.exception()  # 等待方已超时离开时，避免 "exception was never retrieved"


@dataclass
class RunResult:
    """一次 run_all 的结果（LOOK-6827 F'）。

    - run：全部 chunk 成功、或按语言阈值整体跳过的 detector
    - failed：超时 / 抛错 / 有 chunk 失败的 detector；有 chunk 失败时成功 chunk 的 spans 仍在 spans 里
    - skipped：按语言阈值整体跳过的 detector（同时在 run 里）
    - timed_out：超时的 detector（同时在 failed 里）
    """

    spans: list[dict[str, Any]] = field(default_factory=list)
    run: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)


async def run_all(
    detectors: list[Detector],
    text: str,
    timeout_sec: float,
    serial: bool = False,
    limiter: InferenceLimiter | None = None,
    admit: bool = True,
) -> RunResult:
    """跑所有 detector，返回合并前 spans 与各 detector 的执行结果。

    serial=True 时逐个串行执行：CPU 上多个 torch 模型并行会争抢内存带宽/缓存，
    反而更慢甚至超时，串行让每个 detector 独占资源（见 settings.serial_detectors）。
    admit=False 时不占排队名额（调用方已 enter，v2 一个请求多段文本只占一个名额）。
    """

    async def _run_one(d: Detector) -> tuple[str, str, list[dict[str, Any]]]:
        if d.skips(text):
            return d.name, "skipped", []
        try:
            if limiter is not None and d.heavy:
                work = limiter.run(d.detect, text)  # 拿到槽位才创建推理协程
            else:
                work = d.detect(text)
            spans = await asyncio.wait_for(work, timeout=timeout_sec)
            return d.name, "run", spans
        except asyncio.TimeoutError:
            logger.warning(f"detector {d.name} 超时 {timeout_sec}s")
            return d.name, "timeout", []
        except ChunkFailures as exc:
            logger.warning(f"detector {d.name} 部分失败: {exc}")
            return d.name, "failed", exc.spans
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"detector {d.name} 失败: {exc}")
            return d.name, "failed", []

    admitted = limiter is not None and admit
    if admitted:
        limiter.enter()
    try:
        if serial:
            results = [await _run_one(d) for d in detectors]
        else:
            results = await asyncio.gather(*(_run_one(d) for d in detectors))
    finally:
        if admitted:
            limiter.leave()
    out = RunResult()
    for name, status, spans in results:
        out.spans.extend(spans)
        if status in ("failed", "timeout"):
            out.failed.append(name)
            if status == "timeout":
                out.timed_out.append(name)
            continue
        out.run.append(name)
        if status == "skipped":
            out.skipped.append(name)
    return out
