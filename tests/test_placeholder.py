"""placeholder.py 单测：占位符生成 / dedup / 还原 byte-perfect / 回潮检测。"""
from __future__ import annotations

from app.placeholder import build_redacted, normalize_key, restore


def _span(start: int, end: int, text: str, canonical: str = "PERSON") -> dict:
    return {
        "start": start,
        "end": end,
        "text": text,
        "canonical": canonical,
        "raw_label": canonical,
        "source": "hanlp",
        "score": 0.9,
    }


def test_normalize_key_NFKC_lower_strip() -> None:
    """全角 / 空白 / 大小写应被归一"""
    assert normalize_key("  John  ", "PERSON") == normalize_key("john", "PERSON")
    assert normalize_key("ＡＢＣ", "ORG") == normalize_key("ABC", "ORG")  # 全角→半角
    assert normalize_key("a  b", "EMAIL") == normalize_key("a b", "EMAIL")  # 多空格归一


def test_空_spans_返回原文() -> None:
    text = "无任何 PII 的文本"
    redacted, mapping = build_redacted(text, [])
    assert redacted == text
    assert mapping == []


def test_单_span_占位符() -> None:
    text = "我叫张三"
    spans = [_span(2, 4, "张三", "PERSON")]
    redacted, mapping = build_redacted(text, spans)
    assert redacted == "我叫[[PERSON_1]]"
    assert len(mapping) == 1
    assert mapping[0]["placeholder"] == "[[PERSON_1]]"
    assert mapping[0]["original"] == "张三"
    assert mapping[0]["count"] == 1


def test_dedup_同名两次只一个占位符() -> None:
    text = "张三和张三"
    spans = [
        _span(0, 2, "张三", "PERSON"),
        _span(3, 5, "张三", "PERSON"),
    ]
    redacted, mapping = build_redacted(text, spans)
    assert redacted == "[[PERSON_1]]和[[PERSON_1]]"
    assert len(mapping) == 1
    assert mapping[0]["count"] == 2


def test_dedup_NFKC_归一() -> None:
    """全角 vs 半角 vs 大小写应被 dedup 视作同一 token"""
    text = "John和ＪＯＨＮ"
    spans = [
        _span(0, 4, "John", "PERSON"),
        _span(5, 9, "ＪＯＨＮ", "PERSON"),
    ]
    redacted, mapping = build_redacted(text, spans)
    assert len(mapping) == 1
    assert mapping[0]["count"] == 2


def test_不同_canonical_独立编号() -> None:
    text = "张三的邮箱是 a@b.com"
    spans = [
        _span(0, 2, "张三", "PERSON"),
        _span(7, 12, "a@b.com", "EMAIL"),
    ]
    redacted, mapping = build_redacted(text, spans)
    assert "[[PERSON_1]]" in redacted
    assert "[[EMAIL_1]]" in redacted
    placeholders = sorted([m["placeholder"] for m in mapping])
    assert placeholders == ["[[EMAIL_1]]", "[[PERSON_1]]"]


def test_同_canonical_独立递增() -> None:
    text = "张三和李四"
    spans = [
        _span(0, 2, "张三", "PERSON"),
        _span(3, 5, "李四", "PERSON"),
    ]
    redacted, mapping = build_redacted(text, spans)
    assert redacted == "[[PERSON_1]]和[[PERSON_2]]"


def test_restore_byte_perfect() -> None:
    text = "张三和李四，邮箱 a@b.com"
    # 字符索引：张(0)三(1)和(2)李(3)四(4)，(5)邮(6)箱(7) (8)a(9)@(10)b(11).(12)c(13)o(14)m(15)
    spans = [
        _span(0, 2, "张三", "PERSON"),
        _span(3, 5, "李四", "PERSON"),
        _span(9, 16, "a@b.com", "EMAIL"),
    ]
    redacted, mapping = build_redacted(text, spans)
    restored, unresolved = restore(redacted, mapping)
    assert restored == text
    assert unresolved == []


def test_restore_回潮检测() -> None:
    """LLM 改写或杜撰占位符，应在 unresolved 报告"""
    text = "你好 [[PERSON_1]]，已记下 [[EMAIL_99]]"
    mapping = [{
        "placeholder": "[[PERSON_1]]",
        "canonical": "PERSON",
        "original": "张三",
        "count": 1,
    }]
    restored, unresolved = restore(text, mapping)
    assert "张三" in restored
    assert "[[EMAIL_99]]" in restored  # 没还原
    assert unresolved == ["[[EMAIL_99]]"]


def test_restore_无_mapping_仍扫残留() -> None:
    """传 mapping=[] 也应扫出未还原占位符"""
    text = "残留 [[PERSON_1]] 占位"
    restored, unresolved = restore(text, [])
    assert restored == text
    assert unresolved == ["[[PERSON_1]]"]


def test_render_skip_overlap() -> None:
    """build_redacted 按 start 顺序拼接，cursor 之前的 span 应被跳过
    （在 merge_spans 已经处理重叠后这种情况罕见，但 placeholder 层兜底）"""
    text = "ABCDE"
    spans = [
        _span(0, 3, "ABC", "PERSON"),
        _span(2, 4, "CD", "PERSON"),  # 与上一个重叠，应被跳过
    ]
    redacted, _ = build_redacted(text, spans)
    # 第一个 span 渲染后 cursor=3，第二个 start=2 < 3 被跳过
    assert redacted == "[[PERSON_1]]DE"
