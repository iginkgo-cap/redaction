"""parallel.py 单测：asyncio.gather 编排 + 单 detector 超时/异常隔离。"""
from __future__ import annotations

import asyncio

import pytest

from app.detectors.base import ChunkFailures, Detector
from app.parallel import run_all


class _StubDetector(Detector):
    def __init__(self, name: str, spans=None, *, raises=False, sleep=0.0):
        self.name = name
        self._spans = spans or []
        self._raises = raises
        self._sleep = sleep

    async def detect(self, text):
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raises:
            raise RuntimeError(f"{self.name} simulated failure")
        return self._spans


async def test_全部成功_合并_spans() -> None:
    a = _StubDetector("a", [{"start": 0, "end": 1, "source": "a"}])
    b = _StubDetector("b", [{"start": 2, "end": 3, "source": "b"}])
    r = await run_all([a, b], "irrelevant", timeout_sec=1.0)
    assert sorted(r.run) == ["a", "b"]
    assert r.failed == [] and r.skipped == []
    assert len(r.spans) == 2


async def test_单_detector_异常不影响其他() -> None:
    a = _StubDetector("a", [{"start": 0, "end": 1, "source": "a"}])
    b = _StubDetector("b", raises=True)
    c = _StubDetector("c", [{"start": 2, "end": 3, "source": "c"}])
    r = await run_all([a, b, c], "x", timeout_sec=1.0)
    assert sorted(r.run) == ["a", "c"]
    assert r.failed == ["b"]
    assert len(r.spans) == 2


async def test_单_detector_超时记入_failed() -> None:
    a = _StubDetector("a", [{"start": 0, "end": 1, "source": "a"}])
    slow = _StubDetector("slow", [{"start": 5, "end": 6}], sleep=2.0)
    r = await run_all([a, slow], "x", timeout_sec=0.2)
    assert r.run == ["a"]
    assert r.failed == ["slow"]
    assert r.timed_out == ["slow"]
    assert len(r.spans) == 1


async def test_全部失败_返回空() -> None:
    a = _StubDetector("a", raises=True)
    b = _StubDetector("b", raises=True)
    r = await run_all([a, b], "x", timeout_sec=1.0)
    assert r.run == []
    assert r.failed == ["a", "b"]
    assert r.timed_out == []
    assert r.spans == []


async def test_部分_chunk_失败_记入_failed_且保留已成功_spans() -> None:
    class _Partial(Detector):
        name = "p"

        def __init__(self):
            pass

        async def detect(self, text):
            raise ChunkFailures(1, 3, [{"start": 0, "end": 1, "source": "p"}])

    a = _StubDetector("a", [{"start": 2, "end": 3, "source": "a"}])
    r = await run_all([a, _Partial()], "x", timeout_sec=1.0)
    assert r.run == ["a"]
    assert r.failed == ["p"]
    assert len(r.spans) == 2  # 成功 chunk 的结果照常参与合并


async def test_按阈值跳过_记入_run_与_skipped_且不调用_detect() -> None:
    class _Skipper(_StubDetector):
        def skips(self, text):
            return True

        async def detect(self, text):
            raise AssertionError("跳过的 detector 不应被调用")

    r = await run_all([_Skipper("s"), _StubDetector("a")], "x", timeout_sec=1.0)
    assert r.run == ["s", "a"]
    assert r.skipped == ["s"]
    assert r.failed == []


# ---------- 推理准入限制（2026-10-19 OOM 修复）----------
from app.parallel import InferenceLimiter, Overloaded  # noqa: E402


class _HeavyStub(_StubDetector):
    heavy = True

    running = 0
    peak = 0

    async def detect(self, text):
        _HeavyStub.running += 1
        _HeavyStub.peak = max(_HeavyStub.peak, _HeavyStub.running)
        try:
            return await super().detect(text)
        finally:
            _HeavyStub.running -= 1


def _reset_counters() -> None:
    _HeavyStub.running = 0
    _HeavyStub.peak = 0


async def test_heavy_detector_并发不超过上限() -> None:
    _reset_counters()
    limiter = InferenceLimiter(max_concurrent=2, max_pending=100)
    dets = [_HeavyStub("h", [{"start": 0, "end": 1}], sleep=0.05)]
    await asyncio.gather(*(run_all(dets, "x", timeout_sec=5.0, limiter=limiter) for _ in range(10)))
    assert _HeavyStub.peak == 2


async def test_light_detector_不受限() -> None:
    _reset_counters()
    limiter = InferenceLimiter(max_concurrent=1, max_pending=100)
    light = _StubDetector("light", [{"start": 0, "end": 1}], sleep=0.05)
    t0 = asyncio.get_event_loop().time()
    await asyncio.gather(*(run_all([light], "x", timeout_sec=5.0, limiter=limiter) for _ in range(10)))
    assert asyncio.get_event_loop().time() - t0 < 0.3  # 10 个并行跑，不是串行 0.5s


async def test_排队超过上限_抛Overloaded() -> None:
    _reset_counters()
    limiter = InferenceLimiter(max_concurrent=1, max_pending=2)
    dets = [_HeavyStub("h", [], sleep=0.2)]
    tasks = [asyncio.create_task(run_all(dets, "x", timeout_sec=5.0, limiter=limiter)) for _ in range(5)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    overloaded = [r for r in results if isinstance(r, Overloaded)]
    assert len(overloaded) == 3
    assert limiter.pending == 0


async def test_等槽位期间超时_不会启动推理() -> None:
    _reset_counters()
    limiter = InferenceLimiter(max_concurrent=1, max_pending=100)
    holder = _HeavyStub("holder", [], sleep=0.5)
    calls: list[str] = []

    class _Spy(_HeavyStub):
        async def detect(self, text):
            calls.append("ran")
            return await super().detect(text)

    waiter = _Spy("waiter", [{"start": 0, "end": 1}])
    t1 = asyncio.create_task(run_all([holder], "x", timeout_sec=5.0, limiter=limiter))
    await asyncio.sleep(0.01)
    r = await run_all([waiter], "x", timeout_sec=0.1, limiter=limiter)
    assert r.run == [] and r.spans == []
    assert r.failed == ["waiter"]
    assert calls == []  # 等槽位时就超时，推理从未开始（不产生僵尸线程工作）
    await t1


async def test_超时后线程仍在跑_槽位不释放() -> None:
    """wait_for 超时只取消协程，to_thread 里的推理线程继续跑；槽位必须等线程跑完才还，
    否则过载时真实在跑的推理线程数会超过上限。"""
    import threading
    import time

    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    class _ThreadHeavy(Detector):
        name = "t"
        heavy = True

        def __init__(self):
            pass

        def _sync(self):
            with lock:
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
            time.sleep(0.3)
            with lock:
                state["running"] -= 1
            return []

        async def detect(self, text):
            return await asyncio.to_thread(self._sync)

    limiter = InferenceLimiter(max_concurrent=1, max_pending=100)
    d = _ThreadHeavy()
    # 每个请求 0.1s 就超时，但线程要跑 0.3s
    await asyncio.gather(*(run_all([d], "x", timeout_sec=0.1, limiter=limiter) for _ in range(5)))
    await asyncio.sleep(0.5)  # 等残留线程跑完
    assert state["peak"] == 1


async def test_admit_False_不占排队名额() -> None:
    _reset_counters()
    limiter = InferenceLimiter(max_concurrent=1, max_pending=1)
    limiter.enter()  # 调用方已占唯一名额
    dets = [_HeavyStub("h", [{"start": 0, "end": 1}])]
    rs = await asyncio.gather(*(run_all(dets, "x", timeout_sec=5.0, limiter=limiter, admit=False) for _ in range(3)))
    assert all(r.run == ["h"] for r in rs)
    assert limiter.pending == 1
    limiter.leave()
