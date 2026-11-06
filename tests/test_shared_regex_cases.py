"""LOOK-6827：共享测试样例（服务端完整正则链路）。labs 侧跑同一份副本。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.detectors.cn_regex import CnRegexDetector
from app.detectors.en_regex import EnRegexDetector
from app.merge import apply_context_rules, merge_spans

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "shared_regex_cases.json").read_text(encoding="utf-8")
)["cases"]


@pytest.fixture(scope="module")
def detectors(canonical_map, en_rules_path):
    return [CnRegexDetector(canonical_map), EnRegexDetector(canonical_map, en_rules_path)]


def _pipeline(detectors, text: str) -> set[tuple[str, str]]:
    async def run():
        raw: list[dict] = []
        for d in detectors:
            raw.extend(await d.detect(text))
        return raw

    merged = apply_context_rules(merge_spans(asyncio.run(run())), text)
    return {(s["canonical"], s["text"]) for s in merged}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_shared_case(detectors, case):
    got = _pipeline(detectors, case["text"])
    for kind, text in case["include"]:
        assert (kind, text) in got, f"缺少 {kind}:{text}，实际 {sorted(got)}"
    for kind, text in case["exclude"]:
        assert (kind, text) not in got, f"不应识别 {kind}:{text}"
