"""v2 文本处理纯函数：json 项展开、找值位置、选 span、替换（ADR-002 §2.4、§3.2）。

offset 一律相对原输入（Unicode 码点）。原文里已有的占位符（含模型复述时的变形写法）与 [REDACTED:KIND]
所在区间不参与识别与替换（与 labs types.protectedPlaceholderRanges 同口径）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

from ..placeholder import PLACEHOLDER_RE, normalize_key
from ..schemas import CANONICAL_KINDS

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KINDS = "|".join(sorted(CANONICAL_KINDS | {"CUSTOM"}))
# 模型复述时的变形占位符：\[PERSON\_4\]、[PERSON_4]（限定已知 kind + 数字，不误伤普通方括号）
_LOOSE_PLACEHOLDER_RE = re.compile(rf"\\?\[\\?\[?(?:{_KINDS})\\?_\d+\\?\]\\?\]?")
_REDACTED_MARK_RE = re.compile(r"\[REDACTED:[A-Z_]+\]")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]")
# 少于 2 个字符的已知值不替换（单字替换会让正文面目全非，与 labs known-value-redact 一致）
MIN_KNOWN_VALUE_CHARS = 2

# 优先级：known_placeholders > include > 识别结果。重叠时长的先取，等长再按优先级
KNOWN, INCLUDE, DETECTED = 0, 1, 2


@dataclass(frozen=True)
class Segment:
    item: int  # items 下标
    path: str | None  # json 项的 JSONPath；text 项为 None
    text: str


def norm(value: str) -> str:
    return normalize_key(value, "")[0]


def _child_path(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    if _IDENT.match(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _strings(node: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, _child_path(path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, _child_path(path, i))


def flatten(items: list[dict[str, Any]]) -> list[Segment]:
    """items 元素为 {"text": str}、{"json": Any} 或 {"pages": [str | None]}（file 项抽出的每页文本，扫描页为 None）；
    json 只取值里的字符串，不取 key；pages 每页一段，path 为 $.pages[页下标]。"""
    out: list[Segment] = []
    for i, it in enumerate(items):
        if "text" in it:
            out.append(Segment(i, None, it["text"]))
        elif "pages" in it:
            out.extend(Segment(i, f"$.pages[{k}]", t) for k, t in enumerate(it["pages"]) if t is not None)
        else:
            out.extend(Segment(i, p, s) for p, s in _strings(it["json"], "$"))
    return out


def _replace_strings(node: Any, new: Iterator[str]) -> Any:
    if isinstance(node, str):
        return next(new)
    if isinstance(node, dict):
        return {k: _replace_strings(v, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_strings(v, new) for v in node]
    return node


def rebuild(items: list[dict[str, Any]], segments: list[Segment], texts: list[str]) -> list[Any]:
    """按 flatten 的顺序把替换后的文本放回，返回每个 item 的新 text / json；pages 项不在这里写回，给 None。"""
    by_item: dict[int, list[str]] = {}
    for seg, t in zip(segments, texts):
        by_item.setdefault(seg.item, []).append(t)
    out: list[Any] = []
    for i, it in enumerate(items):
        new = iter(by_item.get(i, []))
        if "pages" in it:
            out.append(None)
        else:
            out.append(next(new) if "text" in it else _replace_strings(it["json"], new))
    return out


def placeholder_regions(text: str) -> list[tuple[int, int]]:
    return [m.span() for r in (PLACEHOLDER_RE, _LOOSE_PLACEHOLDER_RE, _REDACTED_MARK_RE) for m in r.finditer(text)]


def find_all(text: str, value: str) -> list[tuple[int, int]]:
    out, start = [], 0
    while value:
        i = text.find(value, start)
        if i < 0:
            break
        out.append((i, i + len(value)))
        start = i + len(value)
    return out


def find_known(text: str, value: str) -> list[tuple[int, int]]:
    """已知值的位置：太短的不替换；首 / 尾是 ASCII 字母数字时要求词边界（「Li」不吃掉「Limited」）。"""
    if len(value.strip()) < MIN_KNOWN_VALUE_CHARS:
        return []

    def joined(i: int, ch: str) -> bool:
        return 0 <= i < len(text) and bool(_ASCII_WORD.match(ch)) and bool(_ASCII_WORD.match(text[i]))

    return [(s, e) for s, e in find_all(text, value) if not joined(s - 1, value[0]) and not joined(e, value[-1])]


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def select(text: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """candidates 带 start/end/priority；去掉与已有占位符重叠的，长的先取（不让短值把长实体切碎漏出明文），
    等长按优先级。"""
    taken = placeholder_regions(text)
    chosen: list[dict[str, Any]] = []
    for c in sorted(candidates, key=lambda c: (c["start"] - c["end"], c["priority"], c["start"])):
        span = (c["start"], c["end"])
        if span[0] >= span[1] or any(_overlaps(span, t) for t in taken):
            continue
        taken.append(span)
        chosen.append(c)
    return sorted(chosen, key=lambda c: c["start"])


def replace(text: str, spans: list[dict[str, Any]], tokens: list[str]) -> str:
    """spans 已按 start 排序且不重叠，tokens 与之一一对应。"""
    pieces, cursor = [], 0
    for s, tok in zip(spans, tokens):
        pieces.append(text[cursor:s["start"]])
        pieces.append(tok)
        cursor = s["end"]
    pieces.append(text[cursor:])
    return "".join(pieces)
