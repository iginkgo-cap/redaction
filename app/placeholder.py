"""占位符生成与还原（仅 Phase 1 stateless demo 用）。

注意：生产链路占位符 N 在 Ginkgo-labs Node 端 `lib/redaction/placeholder.ts` 生成，
本模块仅用于服务端的 /v1/redact 与 /v1/restore endpoint（demo / 自测）。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# 占位符格式：[[KIND_N]]（Obsidian wiki link 风格）
# 选择理由：markdown sanitize 安全 + 完全 ASCII + LLM 训练分布有 wiki link 语义
# 业界 Presidio/LangChain 默认 <KIND_N>，但我方前端走 streamdown + rehype-sanitize
# 会吃掉未知 HTML tag，故妥协走 [[...]] 形态
PLACEHOLDER_FMT = "[[{kind}_{n}]]"
# 解析正则：容忍 LLM 在 markdown 上下文里把 _ 转义成 \_
PLACEHOLDER_RE = re.compile(r"\[\[([A-Z_]+)\\?_(\d+)\]\]")


def normalize_key(text: str, canonical: str) -> tuple[str, str]:
    norm = unicodedata.normalize("NFKC", text).strip().lower()
    norm = re.sub(r"\s+", " ", norm)
    return (norm, canonical)


def build_redacted(
    text: str, spans: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """按 (canonical) 分组单调递增 N，dedup by (NFKC.strip.lower, canonical)。

    返回 (redacted_text, mapping)
      mapping 元素：{placeholder, canonical, original, count}
    """
    if not spans:
        return text, []
    spans_sorted = sorted(spans, key=lambda s: s["start"])
    counters: dict[str, int] = {}
    key_to_token: dict[tuple[str, str], dict[str, Any]] = {}
    mapping: list[dict[str, Any]] = []
    placeholder_per_span: list[str] = []

    for s in spans_sorted:
        key = normalize_key(s["text"], s["canonical"])
        tok = key_to_token.get(key)
        if tok is None:
            counters[s["canonical"]] = counters.get(s["canonical"], 0) + 1
            tok = {
                "placeholder": PLACEHOLDER_FMT.format(kind=s["canonical"], n=counters[s["canonical"]]),
                "canonical": s["canonical"],
                "original": s["text"],
                "count": 0,
            }
            key_to_token[key] = tok
            mapping.append(tok)
        tok["count"] += 1
        placeholder_per_span.append(tok["placeholder"])

    # 按 start 升序拼接，跳过被高优先级覆盖的（merge_spans 已处理）
    pieces: list[str] = []
    cursor = 0
    for s, ph in zip(spans_sorted, placeholder_per_span):
        if s["start"] < cursor:
            continue
        pieces.append(text[cursor:s["start"]])
        pieces.append(ph)
        cursor = s["end"]
    pieces.append(text[cursor:])
    return "".join(pieces), mapping


def restore(text: str, mapping: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """把占位符替换回原文，返回 (restored_text, unresolved_placeholders)"""
    if not mapping:
        # 即使没传 mapping 也扫一遍，识别出残留占位符
        unresolved = sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(text)})
        return text, unresolved

    table: dict[str, str] = {m["placeholder"]: m["original"] for m in mapping}

    def _sub(match: re.Match) -> str:
        ph = match.group(0)
        return table.get(ph, ph)

    out = PLACEHOLDER_RE.sub(_sub, text)
    unresolved = sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(out)})
    return out, unresolved
