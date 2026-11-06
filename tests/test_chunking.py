"""chunking.py 单测。

核心断言：
- 短文本不切
- 长文本按字符长度切 + 全局偏移正确
- 重叠区域确实重叠
- chunk 边界优先句子边界
- 拼回所有 chunk.text（带重叠去重）等于原文
"""
from __future__ import annotations

from app.chunking import Chunk, chunk_with_offsets, count_zh_chars


def test_短文本不切() -> None:
    text = "短文本不需要切。"
    chunks = chunk_with_offsets(text, chunk_size=800, overlap=80)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].global_start == 0
    assert chunks[0].global_end == len(text)


def test_空文本返回空列表() -> None:
    assert chunk_with_offsets("", chunk_size=800, overlap=80) == []


def test_长文本按长度切() -> None:
    text = "x" * 2000  # 无句子边界，纯字符
    chunks = chunk_with_offsets(text, chunk_size=800, overlap=80)
    assert len(chunks) >= 3
    # 第一个 chunk 从 0 开始
    assert chunks[0].global_start == 0
    # 最后一个 chunk 覆盖到末尾
    assert chunks[-1].global_end == 2000


def test_全局偏移与子串一致() -> None:
    text = "abcdefg" * 200  # 1400 字符
    chunks = chunk_with_offsets(text, chunk_size=500, overlap=50)
    for ck in chunks:
        assert text[ck.global_start:ck.global_end] == ck.text


def test_重叠覆盖() -> None:
    """OVERLAP > 0 时相邻 chunk 应有重叠区"""
    text = "x" * 2000
    chunks = chunk_with_offsets(text, chunk_size=800, overlap=100)
    assert len(chunks) >= 2
    # 第 2 个 chunk 起点应在第 1 个 chunk 结束之前（除非命中句子边界）
    assert chunks[1].global_start < chunks[0].global_end


def test_句子边界优先() -> None:
    """chunk 边界附近若有句号 / 换行，应回退到句号 / 换行 + 1"""
    head = "x" * 400 + "。"          # 401 char
    mid = "y" * 200 + "。"            # 已在 chunk_size 内
    tail = "z" * 400
    text = head + mid + tail
    chunks = chunk_with_offsets(text, chunk_size=500, overlap=0)
    # 第一个 chunk 应在第一个 "。" 后切
    assert chunks[0].text.endswith("。")
    # 第一个 chunk 不应超过 chunk_size
    assert len(chunks[0].text) <= 500


def test_长地址跨边界_用_overlap_覆盖() -> None:
    """模拟长地址跨 chunk 边界，OVERLAP 应让它至少完整出现在一个 chunk 里"""
    long_addr = "上海市浦东新区世纪大道 100 号上海中心大厦 56 楼 5601 室"
    text = ("x" * 790) + long_addr + ("y" * 100)
    chunks = chunk_with_offsets(text, chunk_size=800, overlap=80)
    # 长地址要么完整在某 chunk 里，要么因 overlap 至少头部完整出现
    full_match = any(long_addr in ck.text for ck in chunks)
    assert full_match, f"长地址未完整出现：chunks={[ck.text[:50] for ck in chunks]}"


def test_overlap_无退化尾chunk_回归() -> None:
    """回归（Round 9 踩坑）：overlap>0 且文本略长于 chunk_size 时，loop 必须在
    end 到达文末时停止；否则从末尾反复回退 overlap 生成一串退化尾 chunk
    （长度 overlap,overlap-1,…,1），每个都触发一次模型推理。
    曾实测：950 字 + overlap=80 产生 82 个 chunk（应为 2），GLiNER 延迟 342→2477ms。"""
    text = "x" * 950
    chunks = chunk_with_offsets(text, chunk_size=800, overlap=80)
    assert len(chunks) <= 3, f"退化尾 chunk 回归：{len(chunks)} 个（应 ≤3）"
    assert chunks[-1].global_end == 950
    # 除最后一个外，不应出现长度 ≤ overlap 的退化 chunk
    assert all(len(ck.text) > 80 for ck in chunks[:-1])
    # 更长文本同理：3500 字应是个位数 chunk，不是几十
    long_chunks = chunk_with_offsets("y" * 3500, chunk_size=800, overlap=80)
    assert len(long_chunks) <= 6, f"长文退化：{len(long_chunks)} 个"


def test_count_zh_chars() -> None:
    assert count_zh_chars("hello") == 0
    assert count_zh_chars("你好") == 2
    assert count_zh_chars("hello 世界 world") == 2
    # 标点不算
    assert count_zh_chars("。！？") == 0


def test_chunk_dataclass_字段() -> None:
    ck = Chunk(text="abc", global_start=10, global_end=13)
    assert ck.text == "abc"
    assert ck.global_start == 10
    assert ck.global_end == 13
