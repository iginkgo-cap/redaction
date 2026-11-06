"""merge.py 单测：split-based merge 与优先级。

核心断言：
- 高优先级 source（cn_regex）压低优先级（nvidia/en_regex）
- 同优先级按 score / length 排序
- 不重叠 spans 全保留
- 完全包含 → 低优先级丢弃
- 部分重叠 + carved_ratio < 30% → 保留 leftover 段
- 部分重叠 + carved_ratio ≥ 30% → 丢弃所有 leftover
- 小 leftover (< 3 字符) 丢弃
"""
from __future__ import annotations

from app.merge import merge_spans


def _span(start: int, end: int, source: str, score: float = 0.9, canonical: str = "PERSON", text: str = "") -> dict:
    return {
        "start": start,
        "end": end,
        "source": source,
        "score": score,
        "canonical": canonical,
        "raw_label": canonical,
        "text": text or "x" * (end - start),
    }


def test_空输入() -> None:
    assert merge_spans([]) == []


def test_不重叠全保留() -> None:
    spans = [
        _span(0, 5, "nvidia"),
        _span(10, 15, "hanlp"),
        _span(20, 25, "cn_regex"),
    ]
    out = merge_spans(spans)
    assert len(out) == 3
    # 按 start 排序
    assert [s["start"] for s in out] == [0, 10, 20]


def test_完全包含_低优丢弃() -> None:
    """nvidia (0,20) 完全被 cn_regex (0,20) 覆盖 → 只剩 cn_regex"""
    spans = [
        _span(0, 20, "nvidia", score=0.95),
        _span(0, 20, "cn_regex", score=0.5),
    ]
    out = merge_spans(spans)
    assert len(out) == 1
    assert out[0]["source"] == "cn_regex"


def test_高优内嵌_低优分裂保留() -> None:
    """NVIDIA 大 span (0,100) + HanLP 小 span (50,55) 嵌入
    HanLP 优先级高，先入；NVIDIA 被切成 (0,50) + (55,100)
    NVIDIA 仅 5% 被挖（远低于 30% 阈值），两段都保留为 split_from_original"""
    nvidia_text = "a" * 100
    spans = [
        _span(0, 100, "nvidia", score=0.9, text=nvidia_text),
        _span(50, 55, "hanlp", score=0.9, text="bbbbb"),
    ]
    out = merge_spans(spans)
    sources = sorted([s["source"] for s in out])
    # hanlp 1 个 + nvidia 2 段
    assert sources == ["hanlp", "nvidia", "nvidia"]
    nvidia_segs = [s for s in out if s["source"] == "nvidia"]
    starts = sorted([(s["start"], s["end"]) for s in nvidia_segs])
    assert starts == [(0, 50), (55, 100)]
    # 标记 split
    assert all(s.get("split_from_original") for s in nvidia_segs)


def test_carved_ratio_超阈值丢弃_leftover() -> None:
    """NVIDIA 17 字 span (0,17) 被 HanLP 12 字 span (0,10) + PERSON (15,17) 覆盖
    挖掉 12 字 → 留 (10,15) 共 5 字 leftover
    carved_ratio = 12/17 ≈ 71% > 30% → 丢弃 leftover"""
    spans = [
        _span(0, 17, "nvidia", score=0.9, text="x" * 17),
        _span(0, 10, "hanlp", score=0.9, text="y" * 10),
        _span(15, 17, "hanlp", score=0.9, text="zz"),
    ]
    out = merge_spans(spans)
    nvidia_segs = [s for s in out if s["source"] == "nvidia"]
    assert nvidia_segs == [], "carved_ratio > 30% 时应丢弃所有 nvidia leftover"


def test_小_leftover_丢弃() -> None:
    """leftover 段 < 3 字符直接丢弃"""
    spans = [
        _span(0, 100, "nvidia", score=0.9, text="x" * 100),
        _span(2, 99, "hanlp", score=0.9, text="y" * 97),  # 挖 97 字，剩 (0,2) + (99,100) 都 < 3
    ]
    out = merge_spans(spans)
    # hanlp 保留；nvidia 两个小 leftover 都 < 3 字符，丢弃；但 carved_ratio = 97/100 = 97% > 30% 也会丢
    nvidia_segs = [s for s in out if s["source"] == "nvidia"]
    assert nvidia_segs == []


def test_优先级排序() -> None:
    """cn_regex(4) > hanlp(3) > nvidia(2) > en_regex(1)"""
    spans = [
        _span(0, 10, "en_regex", score=0.9, canonical="EMAIL", text="0123456789"),
        _span(0, 10, "hanlp", score=0.9, canonical="EMAIL", text="0123456789"),
        _span(0, 10, "cn_regex", score=0.9, canonical="EMAIL", text="0123456789"),
        _span(0, 10, "nvidia", score=0.9, canonical="EMAIL", text="0123456789"),
    ]
    out = merge_spans(spans)
    assert len(out) == 1
    assert out[0]["source"] == "cn_regex"


def test_同优先级_score_高的胜出() -> None:
    spans = [
        _span(0, 10, "nvidia", score=0.5, text="0123456789"),
        _span(0, 10, "nvidia", score=0.9, text="0123456789"),
    ]
    out = merge_spans(spans)
    assert len(out) == 1
    assert out[0]["score"] == 0.9


def test_split_text_正确截取() -> None:
    """分裂保留的 leftover 段的 text 应是原 text 的对应子串"""
    nvidia_text = "ABCDEFGHIJ"  # 10 字符
    spans = [
        _span(0, 10, "nvidia", score=0.9, text=nvidia_text),
        _span(3, 5, "cn_regex", score=0.95, text="DE"),
    ]
    out = merge_spans(spans)
    nvidia_segs = sorted(
        [s for s in out if s["source"] == "nvidia"],
        key=lambda s: s["start"],
    )
    assert len(nvidia_segs) == 2
    assert nvidia_segs[0]["text"] == "ABC"   # 0..3
    assert nvidia_segs[1]["text"] == "FGHIJ"  # 5..10


def test_F5_USCC_strict_18chars_starting_with_9_reassigns_to_BUSINESS_ID():
    """18 位以 9 开头的字母数字串原本被 cn_regex 当 BANK_CARD / ID_CARD，
    post-process 改写为 BUSINESS_ID。"""
    out = merge_spans([
        _span(0, 18, "cn_regex", canonical="BANK_CARD", text="91110108MA01XKAB23"),
    ])
    assert len(out) == 1
    assert out[0]["canonical"] == "BUSINESS_ID"
    assert out[0]["raw_label"] == "USCC"


def test_F5_USCC_with_X_suffix_also_reassigns():
    out = merge_spans([
        _span(0, 18, "cn_regex", canonical="ID_CARD", text="91310000132245678X"),
    ])
    assert out[0]["canonical"] == "BUSINESS_ID"


def test_F5_mainland_ID_CARD_NOT_reassigned():
    """大陆身份证以 11-65 起头（省级码），绝不会被误改。"""
    out = merge_spans([
        _span(0, 18, "cn_regex", canonical="ID_CARD", text="11010819850512123X"),
    ])
    assert out[0]["canonical"] == "ID_CARD"  # 保持原样


def test_F5_BANK_CARD_19digit_NOT_reassigned():
    """国内银行卡 19 位不应被改（长度不符）。"""
    out = merge_spans([
        _span(0, 19, "cn_regex", canonical="BANK_CARD", text="6228480000123456789"),
    ])
    assert out[0]["canonical"] == "BANK_CARD"


def test_F5_PERSON_span_NOT_touched():
    """非 ID_CARD/BANK_CARD 的 span 即使形态像 USCC 也不动。"""
    out = merge_spans([
        _span(0, 18, "hanlp", canonical="PERSON", text="91110108MA01XKAB23"),
    ])
    assert out[0]["canonical"] == "PERSON"


def test_F5_USCC_with_prefix_wrapper_still_reassigns():
    """NVIDIA over-extraction 场景：span 含 'USCC ' 前缀仍能识别。"""
    out = merge_spans([
        _span(0, 23, "nvidia", canonical="ID_CARD", text="USCC 91110108MA01XKAB23"),
    ])
    assert out[0]["canonical"] == "BUSINESS_ID"


def test_F5_USCC_with_chinese_prefix_wrapper():
    """中文前缀场景：'统一社会信用代码 91...' 整段被吃。"""
    out = merge_spans([
        _span(0, 27, "nvidia", canonical="ID_CARD", text="统一社会信用代码 91120000600123456N"),
    ])
    assert out[0]["canonical"] == "BUSINESS_ID"


def test_F3b_NVIDIA_BANK_CARD_with_BTC_bech32_reassigns_to_CRYPTO_ADDR():
    out = merge_spans([
        _span(0, 42, "nvidia", canonical="BANK_CARD",
              text="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
    ])
    assert out[0]["canonical"] == "CRYPTO_ADDR"


def test_F3b_NVIDIA_BANK_CARD_with_ETH_reassigns():
    out = merge_spans([
        _span(0, 42, "nvidia", canonical="BANK_CARD",
              text="0x742d35Cc6634C0532925a3b844Bc9e7595f0fA1c"),
    ])
    assert out[0]["canonical"] == "CRYPTO_ADDR"


def test_F3b_NVIDIA_BANK_CARD_with_TRX_reassigns():
    out = merge_spans([
        _span(0, 34, "nvidia", canonical="BANK_CARD",
              text="TLPpXqaBSGmEZwgErJMtKDV3xLPocGnv3W"),
    ])
    assert out[0]["canonical"] == "CRYPTO_ADDR"


def test_F3b_real_BANK_CARD_NOT_touched():
    out = merge_spans([
        _span(0, 16, "cn_regex", canonical="BANK_CARD",
              text="4111111111111111"),
    ])
    assert out[0]["canonical"] == "BANK_CARD"


def test_F6_post_NVIDIA_BANK_CARD_long_digits_to_ACCOUNT():
    """NVIDIA 把 21 位长账号标为 BANK_CARD → 后处理改 ACCOUNT"""
    out = merge_spans([
        _span(0, 21, "nvidia", canonical="BANK_CARD",
              text="622188000000012345678"),
    ])
    assert out[0]["canonical"] == "ACCOUNT"


def test_F6_post_BANK_CARD_with_spaces_long_digits():
    out = merge_spans([
        _span(0, 25, "nvidia", canonical="BANK_CARD",
              text="622188 0000 0001 2345 678"),
    ])
    assert out[0]["canonical"] == "ACCOUNT"


def test_F6_post_19_digit_BANK_CARD_NOT_changed():
    out = merge_spans([
        _span(0, 19, "cn_regex", canonical="BANK_CARD",
              text="6228480000123456789"),
    ])
    assert out[0]["canonical"] == "BANK_CARD"


# F7 ORG_PUBLIC（round11 起泛化为 S1 抑制名单，迁至 apply_context_rules 尾部，
# 语义不变；main.py 两个端点 merge 后都会调 apply_context_rules，行为等价）
def test_F7_org_public_chinese_dropped():
    from app.merge import apply_context_rules
    out = apply_context_rules(
        [_span(0, 4, "hanlp", canonical="ORG", text="贵州茅台")], "贵州茅台"
    )
    assert out == []


def test_F7_org_public_english_case_insensitive_dropped():
    from app.merge import apply_context_rules
    text = "HSBC HK 与 Goldman Sachs Asia LLC"
    out = apply_context_rules([
        _span(0, 7, "nvidia", canonical="ORG", text="HSBC HK"),
        _span(10, 32, "nvidia", canonical="ORG", text="Goldman Sachs Asia LLC"),
    ], text)
    assert out == []


def test_F7_org_private_NOT_dropped():
    """虚构 SPV / 家办 / 私募 不在名单 → 保留"""
    from app.merge import apply_context_rules
    text = "BlueOcean Capital 与清源资本"
    out = apply_context_rules([
        _span(0, 17, "nvidia", canonical="ORG", text="BlueOcean Capital"),
        _span(18, 22, "hanlp", canonical="ORG", text="清源资本"),
    ], text)
    assert len(out) == 2


def test_F7_org_public_only_drops_ORG_canonical():
    """如果是 PERSON 而非 ORG 即使文本是 '腾讯' 也不丢"""
    from app.merge import apply_context_rules
    out = apply_context_rules(
        [_span(0, 2, "hanlp", canonical="PERSON", text="腾讯")], "腾讯"
    )
    assert len(out) == 1


def test_F7_merge_spans_no_longer_drops_org_public():
    """merge_spans 层不再做 ORG_PUBLIC drop（职责已上移，防回归误解）"""
    out = merge_spans([
        _span(0, 4, "hanlp", canonical="ORG", text="贵州茅台"),
    ])
    assert len(out) == 1


def test_F9_eth_all_zero_dropped():
    from app.merge import _post_process_crypto_junk
    out = _post_process_crypto_junk([
        _span(0, 42, "en_regex", canonical="CRYPTO_ADDR",
              text="0x0000000000000000000000000000000000000000"),
    ])
    assert out == []


def test_F9_eth_all_digits_no_letters_dropped():
    from app.merge import _post_process_crypto_junk
    out = _post_process_crypto_junk([
        _span(0, 42, "en_regex", canonical="CRYPTO_ADDR",
              text="0x1234567890123456789012345678901234567890"),
    ])
    assert out == []


def test_F9_btc_long_run_dropped():
    from app.merge import _post_process_crypto_junk
    out = _post_process_crypto_junk([
        _span(0, 47, "en_regex", canonical="CRYPTO_ADDR",
              text="bc1qzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzqqqqqxx"),
    ])
    assert out == []


def test_F9_real_eth_preserved():
    """混合大小写的真实 ETH 地址不被误杀"""
    from app.merge import _post_process_crypto_junk
    out = _post_process_crypto_junk([
        _span(0, 42, "en_regex", canonical="CRYPTO_ADDR",
              text="0x742d35cc6634C0532925a3b844bc9e7595f0fa1C"),
    ])
    assert len(out) == 1


def test_F9_real_btc_bech32_preserved():
    from app.merge import _post_process_crypto_junk
    out = _post_process_crypto_junk([
        _span(0, 42, "en_regex", canonical="CRYPTO_ADDR",
              text="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
    ])
    assert len(out) == 1


def test_F8_apply_context_rules_TIN_keyword_promotes_id_card_to_tin():
    from app.merge import apply_context_rules
    text = "香港 TIN AB1234569 已登记"
    spans = [{
        "start": 7, "end": 16, "text": "AB1234569",
        "canonical": "ID_CARD", "raw_label": "national id",
        "source": "nvidia", "score": 0.9,
    }]
    out = apply_context_rules(spans, text)
    assert out[0]["canonical"] == "TIN"


def test_F8_apply_context_rules_no_TIN_keyword_leaves_id_card():
    from app.merge import apply_context_rules
    text = "HKID K987654(5) 已核验"
    spans = [{
        "start": 5, "end": 15, "text": "K987654(5)",
        "canonical": "ID_CARD", "raw_label": "national id",
        "source": "nvidia", "score": 0.9,
    }]
    out = apply_context_rules(spans, text)
    assert out[0]["canonical"] == "ID_CARD"


def test_F8_apply_context_rules_EIN_with_TIN_promoted():
    from app.merge import apply_context_rules
    text = "Entity TIN 98-7654321 confirmed."
    spans = [{
        "start": 11, "end": 21, "text": "98-7654321",
        "canonical": "BUSINESS_ID", "raw_label": "CN_BUSINESS_ID_EIN",
        "source": "cn_regex", "score": 0.8,
    }]
    out = apply_context_rules(spans, text)
    assert out[0]["canonical"] == "TIN"


def test_F8_apply_context_rules_window_size_limits_match():
    """TIN 太远（> 30 字符前）不触发"""
    from app.merge import apply_context_rules
    text = "TIN reported earlier................................AB1234569"
    spans = [{
        "start": text.index("AB"), "end": text.index("AB") + 9,
        "text": "AB1234569", "canonical": "ID_CARD",
        "raw_label": "national id", "source": "nvidia", "score": 0.9,
    }]
    out = apply_context_rules(spans, text)
    assert out[0]["canonical"] == "ID_CARD"


def test_F7_NFKC_fullwidth_HSBC_matched():
    """全角字母 ＨＳＢＣ 经 NFKC 归一后等价于 HSBC，应当被名单 drop"""
    out = apply_context_rules([
        _span(0, 4, "nvidia", canonical="ORG", text="ＨＳＢＣ"),
    ], "ＨＳＢＣ")
    assert out == [], f"expected drop, got {out!r}"


def test_F7_NFKC_fullwidth_spaces_trimmed():
    """全角空格 + 大小写混合 仍被 normalize 到名单值"""
    out = apply_context_rules([
        _span(0, 12, "nvidia", canonical="ORG", text="　HSBC HK　"),
    ], "　HSBC HK　")
    assert out == []


def test_F7_NFKC_unknown_org_still_kept():
    """NFKC 归一后仍不在名单的 ORG 不动"""
    out = apply_context_rules([
        _span(0, 10, "nvidia", canonical="ORG", text="ＲａｎｄｏｍＣｏ"),
    ], "ＲａｎｄｏｍＣｏ")
    assert len(out) == 1


# ─── F10b 相邻 ADDRESS 桥接 ───
from app.merge import apply_context_rules  # noqa: E402


def test_相邻ADDRESS连接字间隔_桥接合并() -> None:
    text = "徐汇区 漕溪北路"  # 两段 ADDRESS，gap=空格（连接字）→ 应缝合
    spans = [
        _span(0, 3, "hanlp", canonical="ADDRESS", text="徐汇区"),
        _span(4, 8, "hanlp", canonical="ADDRESS", text="漕溪北路"),
    ]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 1
    assert out[0]["text"] == "徐汇区 漕溪北路"


def test_相邻ADDRESS非连接字间隔_不合并() -> None:
    text = "徐汇区的漕溪北路"  # gap="的" 非连接字 → 不合并
    spans = [
        _span(0, 3, "hanlp", canonical="ADDRESS", text="徐汇区"),
        _span(4, 8, "hanlp", canonical="ADDRESS", text="漕溪北路"),
    ]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 2


def test_非ADDRESS_不受桥接影响() -> None:
    text = "张三 13800138000"
    spans = [
        _span(0, 2, "hanlp", canonical="PERSON", text="张三"),
        _span(3, 14, "cn_regex", canonical="PHONE", text="13800138000"),
    ]
    out = apply_context_rules(spans, text)
    assert len(out) == 2


# ─── 地址尾部补全：桥接/正则因夹楼宇名被打断后，向右吸收「楼/层/室/单元」尾巴 ───


def test_桥接后补全楼室尾巴() -> None:
    # 报告 case：NER 把「上海中心大厦」「56」切成两段 ADDRESS，桥接成「…大厦 56」，
    # 但尾部「楼 5601 室」无人认领 → 应被尾部补全吸收。
    text = "上海中心大厦 56 楼 5601 室"
    spans = [
        _span(0, 6, "hanlp", canonical="ADDRESS", text="上海中心大厦"),
        _span(7, 9, "hanlp", canonical="ADDRESS", text="56"),
    ]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 1
    assert out[0]["text"] == "上海中心大厦 56 楼 5601 室"
    assert out[0]["end"] == len(text)


def test_单ADDRESS_span_也补全尾巴() -> None:
    # 单段 ADDRESS（len<2，桥接会提前返回）也要补全尾巴。
    text = "浦东新区世纪大道 100 号 502 室"
    spans = [
        _span(0, 14, "cn_regex", canonical="ADDRESS", text="浦东新区世纪大道 100 号"),
    ]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 1
    assert out[0]["text"] == "浦东新区世纪大道 100 号 502 室"


def test_多段尾巴_单元室连续吸收() -> None:
    text = "中山路 5 号 3 单元 502 室"
    spans = [
        _span(0, 7, "cn_regex", canonical="ADDRESS", text="中山路 5 号"),
    ]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 1
    assert out[0]["text"] == "中山路 5 号 3 单元 502 室"


def test_FP守护_号后电话不被吸收() -> None:
    # 「号」后紧跟 11 位电话（无单位字结尾）→ 不得吸收进地址。
    text = "世纪大道 100 号 13800138000"
    spans = [
        _span(0, 10, "cn_regex", canonical="ADDRESS", text="世纪大道 100 号"),
    ]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 1
    assert out[0]["text"] == "世纪大道 100 号"
    assert out[0]["end"] == 10


# ─── F10d 地址前导裁剪：NER 把地址前的引导词（住/现住/位于/地址：…）并进 span 起点 ───
# 与 F10c 尾部补全对称（一头一尾）；仅裁固定引导词且其后紧跟地理行政字（lookahead 守 FP）。


def test_前导住字裁剪() -> None:
    # 报告 case：原文「现住上海市…」，NER 丢「现」却把动词「住」并进 ADDRESS 起点。
    text = "现住上海市浦东新区世纪大道100号"
    spans = [
        _span(1, len(text), "hanlp", canonical="ADDRESS",
              text="住上海市浦东新区世纪大道100号"),
    ]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 1
    assert out[0]["text"] == "上海市浦东新区世纪大道100号"
    assert out[0]["start"] == 2


def test_前导现住整段裁剪() -> None:
    # NER 把引导词「现住」整段并进 span（非只留"住"）→ 也应裁到地理头。
    text = "现住上海市浦东新区世纪大道100号"
    spans = [_span(0, len(text), "gliner", canonical="ADDRESS", text=text)]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 1
    assert out[0]["text"] == "上海市浦东新区世纪大道100号"
    assert out[0]["start"] == 2


def test_前导家住裁剪() -> None:
    text = "家住北京市朝阳区建国路1号"
    spans = [_span(0, len(text), "hanlp", canonical="ADDRESS", text=text)]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert out[0]["text"] == "北京市朝阳区建国路1号"


def test_前导地址标签裁剪() -> None:
    text = "地址：北京市朝阳区建国路1号"
    spans = [
        _span(0, len(text), "gliner", canonical="ADDRESS", text=text),
    ]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert len(out) == 1
    assert out[0]["text"] == "北京市朝阳区建国路1号"


def test_无前导_不裁剪() -> None:
    text = "上海市浦东新区世纪大道100号"
    spans = [_span(0, len(text), "cn_regex", canonical="ADDRESS", text=text)]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert out[0]["text"] == text
    assert out[0]["start"] == 0


def test_FP守护_居字开头地名不裁剪() -> None:
    # 「居安路」的「居」是地名一部分、非引导动词（bare「居」不在引导词表）→ 不裁。
    text = "居安路100号"
    spans = [_span(0, len(text), "cn_regex", canonical="ADDRESS", text=text)]
    out = [s for s in apply_context_rules(spans, text) if s["canonical"] == "ADDRESS"]
    assert out[0]["text"] == text


def test_FP守护_引导词后非地理不裁剪() -> None:
    # 引导词「位于」后非地理行政字 → lookahead 不命中 → 不裁。
    # round11 起该垃圾 span 会被尾部 S3 粒度门控 drop（无结构要素短 span），
    # 故 F10d 守护改在函数层断言（裁剪逻辑本身不动它）。
    from app.merge import _trim_address_head
    text = "位于公司内部"
    spans = [_span(0, len(text), "gliner", canonical="ADDRESS", text=text)]
    out = _trim_address_head(spans, text)
    assert out[0]["text"] == text  # 未被裁剪
    # 端到端：S3 判定其无地址粒度，整体 drop（不脱敏）
    assert apply_context_rules(
        [_span(0, len(text), "gliner", canonical="ADDRESS", text=text)], text
    ) == []
