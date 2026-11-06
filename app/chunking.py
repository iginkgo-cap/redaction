"""文本分块工具：固定字符长度 + 句子边界对齐 + 小幅重叠 + 全局偏移。

详见 docs/features/redaction/2026-05-19-pii-redaction-implementation.md §1.11.3
"""
from __future__ import annotations

from dataclasses import dataclass

SENTENCE_BREAKS = "。！？\n；;.!?"


@dataclass
class Chunk:
    text: str
    global_start: int
    global_end: int


def chunk_with_offsets(
    text: str,
    chunk_size: int,
    overlap: int = 0,
) -> list[Chunk]:
    """切 chunk，每个 chunk 记录全局偏移 (global_start, global_end)。

    - chunk_size：单 chunk 最大字符数
    - overlap：重叠字符数，覆盖跨边界的 PII（建议 80）
    - 优先在句子边界（句号 / 换行 / 分号等）回退
    """
    if not text:
        return []
    if len(text) <= chunk_size:
        return [Chunk(text=text, global_start=0, global_end=len(text))]

    chunks: list[Chunk] = []
    pos = 0
    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        # 往前找最近的句子边界
        if end < len(text):
            search_lower = max(pos + chunk_size // 2, end - 100)
            # 闭区间 [search_lower, end-1]：包含 search_lower 自身，且 end 不能越界
            for i in range(end - 1, search_lower - 1, -1):
                if text[i] in SENTENCE_BREAKS:
                    end = i + 1
                    break
        chunks.append(Chunk(text=text[pos:end], global_start=pos, global_end=end))
        # 到达文末即停（关键修复）：否则 overlap>0 时，最后一个 chunk 的 end=len(text)，
        # next_pos=max(end-overlap, pos+1) 会从末尾反复回退 overlap 生成一串退化尾 chunk
        # （长度 overlap,overlap-1,…,1），每个都触发一次模型推理 → 慢 ~overlap 倍。
        # 实测：950 字 + overlap=80 曾产生 82 个 chunk（应为 2），GLiNER 延迟 342ms→2477ms。
        if end >= len(text):
            break
        # 下一个 chunk 起点 = end - overlap
        next_pos = max(end - overlap, pos + 1)
        if next_pos >= len(text):
            break
        pos = next_pos
    return chunks


def count_zh_chars(text: str) -> int:
    return sum(1 for c in text if "一" <= c <= "鿿")
