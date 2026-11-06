"""Detector 基类。所有 detector 实现 detect(text) → list[dict]。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ChunkFailures(Exception):
    """部分 chunk 推理失败（LOOK-6827 F'）。

    spans 是成功 chunk 的结果，照常参与合并（多打码不漏）；run_all 把该 detector
    记入 detectors_failed，客户端据此知道识别不完整。
    """

    def __init__(self, failed: int, total: int, spans: list[dict[str, Any]]):
        super().__init__(f"{failed}/{total} chunk 失败")
        self.spans = spans


class Detector(ABC):
    name: str = ""  # subclass override
    heavy: bool = False  # True = 模型推理（受 parallel.InferenceLimiter 并发限制）

    def __init__(self, canonical_map):  # noqa: ANN001  避免循环导入
        self.canonical_map = canonical_map

    @property
    def ready(self) -> bool:
        """模型是否已加载。无模型的 detector 默认 True。"""
        return True

    def skips(self, text: str) -> bool:
        """按语言阈值整体跳过本文本（不算失败）。默认不跳过。"""
        return False

    @abstractmethod
    async def detect(self, text: str) -> list[dict[str, Any]]:
        """返回全局坐标的 span 列表"""
        ...
