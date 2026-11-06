"""文本打码：与 PDF 引擎同一套匹配规则（大小写折叠、去空白、长词优先、不重叠）。"""
from __future__ import annotations

from dataclasses import dataclass

from file_masker.engine import Rule, find_hits


@dataclass(frozen=True)
class _Char:
    c: str


def _hits(text: str, rules: list[Rule]) -> list[tuple[list[int], Rule]]:
    return find_hits([_Char(c) for c in text], rules)


def mask_segments(segments: list[str], rules: list[Rule]) -> tuple[list[str], int]:
    """把多段文本当作一段连续文本匹配（如 HTML 里被标签拆开的文字）。
    替换文字写进命中起点所在的段，其余命中字符删除；返回（各段打码结果, 命中数）。"""
    out = [c for seg in segments for c in seg]
    hits = _hits("".join(out), rules)
    for idx, rule in hits:
        first, last = idx[0], idx[-1]
        out[first] = rule.target
        for i in range(first + 1, last + 1):
            out[i] = ""
    result, pos = [], 0
    for seg in segments:
        result.append("".join(out[pos:pos + len(seg)]))
        pos += len(seg)
    return result, len(hits)


def mask_text(text: str, rules: list[Rule]) -> tuple[str, int]:
    """返回（打码后的文本, 命中数）。命中范围整体替换为 rule.target。"""
    [out], hits = mask_segments([text], rules)
    return out, hits


def find_residuals(text: str, rules: list[Rule]) -> int:
    """打码后再扫一遍，返回仍命中的次数。"""
    return len(_hits(text, rules))
