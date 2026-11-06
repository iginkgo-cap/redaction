"""EN / 通用正则识别器：从 en_regex_rules.yaml 加载 8 条家办金融场景规则。

借鉴自 Microsoft Presidio (MIT) 的 PatternRecognizer，仅 port 正则字符串与
validator 调用约定，不引 presidio-analyzer 依赖。
详见 ATTRIBUTIONS.md
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

import regex as re_lib
import yaml

from .base import Detector

logger = logging.getLogger(__name__)


def _resolve_validator(name: str):
    """通过 python-stdnum 解析 validator。约定：模块名 → .is_valid 函数。"""
    if not name:
        return None
    try:
        mod = importlib.import_module(f"stdnum.{name}")
        fn = getattr(mod, "is_valid", None)
        if fn is None:
            logger.warning(f"validator stdnum.{name} 无 is_valid 函数")
            return None
        return fn
    except ImportError:
        logger.warning(f"validator stdnum.{name} 未安装")
        return None


class EnRegexDetector(Detector):
    name = "en_regex"

    def __init__(self, canonical_map, rules_yaml: Path):
        super().__init__(canonical_map)
        with open(rules_yaml, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self.rules: list[dict[str, Any]] = []
        for r in data.get("rules", []):
            canonical = canonical_map.lookup("en_regex", r["name"])
            if canonical is None:
                logger.warning(f"en_regex rule '{r['name']}' 无 canonical 映射，跳过")
                continue
            flags = 0
            for f_name in (r.get("flags") or "").split("|"):
                if f_name.strip().upper() == "IGNORECASE":
                    flags |= re_lib.IGNORECASE
            try:
                compiled = re_lib.compile(r["pattern"], flags=flags)
            except re_lib.error as exc:
                logger.error(f"en_regex rule '{r['name']}' 编译失败: {exc}")
                continue
            self.rules.append({
                "name": r["name"],
                "canonical": canonical,
                "pattern": compiled,
                "validator": _resolve_validator(r.get("validator", "")),
                "score": float(r.get("score", 0.8)),
            })

    async def detect(self, text: str) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for rule in self.rules:
            for m in rule["pattern"].finditer(text):
                t = m.group(0)
                if rule["validator"]:
                    try:
                        # 部分 validator 期待去掉分隔符（如信用卡的空格）
                        cleaned = "".join(ch for ch in t if ch.isalnum())
                        if not rule["validator"](cleaned):
                            continue
                    except Exception:  # noqa: BLE001  validator 内部异常视作不通过
                        continue
                spans.append({
                    "source": "en_regex",
                    "raw_label": rule["name"],
                    "canonical": rule["canonical"],
                    "start": m.start(),
                    "end": m.end(),
                    "text": t,
                    "score": rule["score"],
                })
        return spans
