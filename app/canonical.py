"""canonical 映射加载与查询。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class CanonicalMap:
    def __init__(self, config_path: Path):
        with open(config_path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f)
        self.hanlp: dict[str, str] = data.get("hanlp", {})
        self.nvidia: dict[str, str] = data.get("nvidia", {})
        self.cn_regex: dict[str, str] = data.get("cn_regex", {})
        self.en_regex: dict[str, str] = data.get("en_regex", {})
        self.gazetteer: dict[str, str] = data.get("gazetteer", {})  # S7 round11
        self.hanlp_role_blacklist: set[str] = set(data.get("hanlp_role_blacklist", []))

    def lookup(self, source: str, raw_label: str) -> str | None:
        table = getattr(self, source, None)
        if not isinstance(table, dict):
            return None
        return table.get(raw_label)

    @property
    def nvidia_labels(self) -> list[str]:
        """传入 gliner.predict_entities 的 label 列表"""
        return list(self.nvidia.keys())
