"""precision.py 单测（round11 S 系列精度规则）。

覆盖：
- S1 SuppressionList：kind 段 / "*" 段 / NFKC 归一 / 缺失文件退化
- S2 PERSON 垃圾 span：超长/句读/单字/纯小写/缩写/称谓/公司后缀 + 真名不误杀
- S3 ADDRESS 粒度门控：纯数字/无结构短地名 drop + 结构化地址保留
- S4 DOB gate：出生引导词命中/未命中/后置写法/未来年份
- S6 SWIFT_BIC gate：真实码 + 上下文保留；ATTESTATION（生产实锤）拒识
用例数据来源：生产导出 2026-07-02（字典 387 条 + exclusion 182 条）。
"""
from __future__ import annotations

from pathlib import Path

from app.precision import (
    SuppressionList,
    _is_address_junk,
    _is_person_junk,
    apply_precision_rules,
)

_EMPTY = SuppressionList()  # 结构规则测试默认不带名单，隔离变量


def _span(
    text: str,
    canonical: str,
    start: int = 0,
    source: str = "hanlp",
    raw_label: str | None = None,
    score: float = 0.9,
) -> dict:
    return {
        "start": start,
        "end": start + len(text),
        "text": text,
        "canonical": canonical,
        "raw_label": raw_label or canonical,
        "source": source,
        "score": score,
    }


# ─── S1 SuppressionList ───


def test_S1_kind段命中_drop() -> None:
    sup = SuppressionList({"PERSON": {"福布斯"}})
    text = "福布斯报道了这笔交易"
    out = apply_precision_rules([_span("福布斯", "PERSON")], text, sup)
    assert out == []


def test_S1_kind不匹配_保留() -> None:
    sup = SuppressionList({"ORG": {"福布斯"}})
    text = "福布斯报道了这笔交易"
    out = apply_precision_rules([_span("福布斯", "PERSON")], text, sup)
    assert len(out) == 1


def test_S1_星号段_对所有kind生效() -> None:
    sup = SuppressionList({"*": {"openai"}})
    text = "OpenAI 发布新模型"
    out = apply_precision_rules(
        [_span("OpenAI", "PERSON"), _span("OpenAI", "ORG", start=20)],
        text + " " * 30,
        sup,
    )
    assert out == []


def test_S1_NFKC与大小写归一() -> None:
    sup = SuppressionList.from_yaml(
        Path(__file__).resolve().parent.parent / "config" / "suppression-list.yaml"
    )
    assert sup.hit("ORG", "ＨＳＢＣ")  # 全角 → NFKC → hsbc
    assert sup.hit("ADDRESS", "亚洲")
    assert not sup.hit("ORG", "清源资本")


def test_S1_类型名本身_任何kind都不当实体() -> None:
    # 生产 look 的 [[PERSON_473]] 值就是 "PERSON"：占位符碎片里的类型名被 NER 识别成人名
    from app.schemas import CANONICAL_KINDS

    sup = SuppressionList.from_yaml(
        Path(__file__).resolve().parent.parent / "config" / "suppression-list.yaml"
    )
    for kind in sorted(CANONICAL_KINDS | {"CUSTOM"}):
        assert sup.hit("PERSON", kind), kind
        assert sup.hit("ORG", kind.lower()), kind


def test_S1_补充名单合并进主名单(tmp_path) -> None:
    # LOOK-5231：内部专用条目放 suppression-list.internal.yaml（不外发），存在就合并
    main = tmp_path / "main.yaml"
    main.write_text("suppress:\n  PERSON:\n    - 福布斯\n")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("suppress:\n  PERSON:\n    - 某公司\n  ORG:\n    - acme\n")
    sup = SuppressionList.from_yaml(main, overlays=[overlay])
    assert sup.hit("PERSON", "福布斯") and sup.hit("PERSON", "某公司") and sup.hit("ORG", "ACME")


def test_S1_补充名单缺失_照常只用主名单(tmp_path) -> None:
    main = tmp_path / "main.yaml"
    main.write_text("suppress:\n  PERSON:\n    - 福布斯\n")
    sup = SuppressionList.from_yaml(main, overlays=[tmp_path / "absent.yaml"])
    assert sup.hit("PERSON", "福布斯")


def test_S1_进程单例会加载补充名单(tmp_path, monkeypatch) -> None:
    from app import precision
    from app.settings import settings

    (tmp_path / "suppression-list.yaml").write_text("suppress:\n  PERSON:\n    - 福布斯\n")
    (tmp_path / "suppression-list.internal.yaml").write_text("suppress:\n  PERSON:\n    - 某公司\n")
    monkeypatch.setattr(settings, "config_dir", tmp_path)
    monkeypatch.setattr(precision, "_SUPPRESSION", None)
    assert precision.get_suppression_list().hit("PERSON", "某公司")
    monkeypatch.setattr(precision, "_SUPPRESSION", None)


def test_S1_配置缺失_空名单不炸() -> None:
    sup = SuppressionList.from_yaml(Path("/nonexistent/suppression.yaml"))
    assert not sup.hit("PERSON", "任意值")


# ─── S2 PERSON 垃圾 span（数据：生产字典 223 条 PERSON 误报模式）───


def test_S2_单字姓氏碎片_drop() -> None:
    for t in ["李", "张", "王", "s"]:
        assert _is_person_junk(t), t


def test_S2_超长整句_drop() -> None:
    assert _is_person_junk("每次对话都要你重新解释规则，行为不一致")
    assert _is_person_junk("entity wholly owned by myself")  # 5 词


def test_S2_含句读换行_drop() -> None:
    assert _is_person_junk("my grammar informal\n\n丽丽UBS")
    assert _is_person_junk("Rachel & Marcus")
    assert _is_person_junk("**有了它**")


def test_S2_纯小写功能词_drop() -> None:
    for t in ["for", "vs", "wikis", "attachments", "my grammar"]:
        assert _is_person_junk(t), t


def test_S2_纯小写真名_放行() -> None:
    # 脏文本（转录/OCR）里的小写**全名**不误杀：多 token 且全部已知名才放行
    for t in ["john smith", "yan xuan"]:
        assert not _is_person_junk(t), t


def test_S2_单token小写_即使是名单词也drop() -> None:
    # 生产实证：单 token 小写（million/figure/lili）即使撞名单也是误报主场
    for t in ["million", "figure", "lili", "shanshan"]:
        assert _is_person_junk(t), t


def test_S2_全大写缩写_drop() -> None:
    # "OR"（Or 为罕见姓，与全大写单姓宽免碰撞）移交 suppression overlay，见下
    for t in ["ZT", "WTZ", "HKD", "HKEX", "CRS", "KPMG"]:
        assert _is_person_junk(t), t


def test_S2_全大写单姓_KYC表单_保留() -> None:
    # review P2 修订：Surname: WANG 表单体的全大写真姓（名单∪拼音已知）不误杀
    for t in ["WANG", "CHEN", "NG", "ZHAO"]:
        assert not _is_person_junk(t), t


def test_S2_OR碰撞_走suppression() -> None:
    sup = SuppressionList({"PERSON": {"or"}})
    out = apply_precision_rules([_span("OR", "PERSON")], "HKD OR USD", sup)
    assert out == []


def test_S2_多词长真名_不误杀() -> None:
    # review P1 修订：超 4 词/超 30 字符的真名先过名单复核再判垃圾
    for t in [
        "Juan Carlos de la Cruz",            # 西语 5 词（含小词 de/la）
        "Krishnamurthy Venkatasubramanian",  # 南亚 32 字符
        "Mary Jane Von Der Berg",            # 荷语小词 von/der
    ]:
        assert not _is_person_junk(t), t


def test_S2_超长句子_仍drop() -> None:
    # 复核不放水：句子必含常用词/小写非名 token
    assert _is_person_junk("Please Review The Attached Document For Details")


def test_S2_姓氏Co_不按公司后缀杀() -> None:
    # review P2 修订：菲华常见姓 Co（前 token 全部已知名 + 裸 co 无缩写点）
    assert not _is_person_junk("Jonathan Co")
    assert _is_person_junk("Acme Co")     # 前 token 非已知名 → 公司
    assert _is_person_junk("Trading Co.")  # 带缩写点 → 公司


def test_S2_混杂token_已知英文名_保留() -> None:
    # review P2 修订：1-2 CJK 姓 + 已知英文名的长混杂 token 是合法混排人名
    for t in ["李Michael", "陈Christina"]:
        assert not _is_person_junk(t), t


def test_S2_中文称谓_drop() -> None:
    for t in ["李总", "王总", "张经理", "刘老师", "潘先生"]:
        assert _is_person_junk(t), t


def test_S2_称谓边界_张总统不误杀() -> None:
    assert not _is_person_junk("张总统")  # fullmatch 守护


def test_S2_英文称谓加姓_drop() -> None:
    for t in ["Mr. Wang", "Mr Wang", "Ms Smith", "Dr. Chen"]:
        assert _is_person_junk(t), t


def test_S2_公司后缀_drop() -> None:
    assert _is_person_junk("International Inc.")
    assert _is_person_junk("BlueOcean Holdings")


def test_S2_含数字_drop() -> None:
    assert _is_person_junk("Section 2")
    assert _is_person_junk("王明3号")


def test_S2_中英混杂长token_drop() -> None:
    assert _is_person_junk("enex在这张截图里的ready")


def test_S2_中英混杂短昵称_保留() -> None:
    for t in ["阿May", "李Peter"]:
        assert not _is_person_junk(t), t


def test_S2_英文名加中文姓_保留() -> None:
    # round11 评测跨轮 case 实证回归：token 各自纯 script 的合法人名不得误杀
    for t in ["Patrick 黄", "Bruce 王明", "Cindy 张"]:
        assert not _is_person_junk(t), t


# ─── S2 salvage：junk span 内粘连真名抢救（review P1 修订）───


def test_S2_salvage_顿号粘连中文名_拆分保留() -> None:
    text = "尽调材料请转给张三、李四两位复核。"
    out = apply_precision_rules([_span("张三、李四", "PERSON", start=7)], text, _EMPTY)
    assert [(s["text"], s["start"], s["end"]) for s in out] == [
        ("张三", 7, 9),
        ("李四", 10, 12),
    ]


def test_S2_salvage_括号中英混排_拆分保留() -> None:
    text = "联系人王小明(David)已确认。"
    out = apply_precision_rules([_span("王小明(David)", "PERSON", start=3)], text, _EMPTY)
    assert [(s["text"], s["start"]) for s in out] == [("王小明", 3), ("David", 7)]


def test_S2_salvage_ampersand双英文名_拆分保留() -> None:
    text = "Rachel & Marcus will host the review."
    out = apply_precision_rules([_span("Rachel & Marcus", "PERSON")], text, _EMPTY)
    assert [(s["text"], s["start"]) for s in out] == [("Rachel", 0), ("Marcus", 9)]


def test_S2_salvage_整句junk_无姓名实证_全drop() -> None:
    # 生产整句噪音的拦截力不回退：句子碎片（首字非常用姓氏/含常用词）不抢救
    for junk in [
        "每次对话都要你重新解释规则，非常烦",
        "my grammar informal\n\n丽丽UBS",
        "**有了它**",
    ]:
        out = apply_precision_rules([_span(junk, "PERSON")], junk, _EMPTY)
        assert out == [], junk


def test_S2_salvage_结果仍过S1名单() -> None:
    sup = SuppressionList({"PERSON": {"李四"}})
    text = "张三、李四"
    out = apply_precision_rules([_span("张三、李四", "PERSON")], text, sup)
    assert [s["text"] for s in out] == ["张三"]


def test_S2_真实人名_不误杀() -> None:
    for t in [
        "李安", "闫冠宇", "张明远",           # 中文 2-4 字
        "Philip", "Priscilla", "Maggie",      # 英文单名（首字母大写）
        "Yin Wang", "Yuhui Zuo",              # 拼音全名
        "Jean-Pierre", "O'Brien", "J. K. Rowling",  # 合法人名符号
        "迈克尔·乔丹",                         # 间隔号外文译名
    ]:
        assert not _is_person_junk(t), t


# ─── S3 ADDRESS 粒度门控（数据：生产字典 48 条 ADDRESS 中 32 条粗粒度）───


def test_S3_纯数字_drop() -> None:
    for t in ["29", "7", "0", "1001", "12306", "32"]:
        assert _is_address_junk(t), t


def test_S3_无结构要素短地名_drop() -> None:
    for t in ["北京南站", "太清宫", "白石山", "野三坡", "香港", "長沙", "IPO"]:
        assert _is_address_junk(t), t


def test_S3_含结构要素_保留() -> None:
    for t in [
        "中山路", "大学路", "文化街",              # 道路字
        "东城区安定门东大街2号",
        "上海市浦东新区世纪大道100号 12楼1203室",
        "公馆358号楼1单元",
        "86, Merchants Street, Valletta",       # 英文 Street
    ]:
        assert not _is_address_junk(t), t


def test_S3_长地名无结构字_保留() -> None:
    # >6 字的地名信息量足够，不武断 drop（如马耳他地名）
    assert not _is_address_junk("Il-Bahar Malta Area")


# ─── S4 DOB gate（数据：生产 18 条 DOB 仅 2 条真实生日）───


def test_S4_有出生引导词_保留() -> None:
    text = "出生日期：1988年4月17日"
    span = _span("1988年4月17日", "DOB", start=5, source="cn_regex", raw_label="CN_DOB")
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1


def test_S4_后置出生写法_保留() -> None:
    text = "1994年7月28日出生于大连"
    span = _span("1994年7月28日", "DOB", start=0, source="cn_regex", raw_label="CN_DOB")
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1


def test_S4_无引导词的日期_drop() -> None:
    text = "会议定于2026年6月11日举行"
    span = _span("2026年6月11日", "DOB", start=4, source="cn_regex", raw_label="CN_DOB")
    out = apply_precision_rules([span], text, _EMPTY)
    assert out == []


def test_S4_英文日期无引导词_drop() -> None:
    text = "The deadline is February 9, 2026 for all submissions"
    span = _span("February 9, 2026", "DOB", start=16, source="en_regex", raw_label="DOB_EN")
    out = apply_precision_rules([span], text, _EMPTY)
    assert out == []


def test_S4_英文日期带born_保留() -> None:
    text = "She was born on May 12 1985 in Shanghai"
    span = _span("May 12 1985", "DOB", start=16, source="en_regex", raw_label="DOB_EN")
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1


def test_S4_未来年份即使带生日词_drop() -> None:
    text = "生日：2027年12月31日"
    span = _span("2027年12月31日", "DOB", start=3, source="cn_regex", raw_label="CN_DOB")
    out = apply_precision_rules([span], text, _EMPTY)
    assert out == []


def test_S4_生于引导词_保留() -> None:
    # good case 走查补上的缺口：「生于」不在首版引导词表
    text = "委托人生于1990年3月5日，年龄核验通过"
    span = _span("1990年3月5日", "DOB", start=5, source="cn_regex", raw_label="CN_DOB")
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1


def test_S4_生效不误触发生字() -> None:
    # 「生于」显式列入而非裸「生」——生效/生成里的「生」不得当引导词
    text = "本合同自2026年6月11日生效"
    span = _span("2026年6月11日", "DOB", start=4, source="cn_regex", raw_label="CN_DOB")
    out = apply_precision_rules([span], text, _EMPTY)
    assert out == []


# ─── S6 SWIFT_BIC gate（生产实锤：ATTESTATION 通过 stdnum.bic 被当 SWIFT）───


def test_S6_真实BIC_带上下文_保留() -> None:
    text = "汇款请用 SWIFT 代码 CMBCHKHHXXX 到我司账户"
    span = _span(
        "CMBCHKHHXXX", "BANK_CARD", start=text.index("CMBC"),
        source="en_regex", raw_label="SWIFT_BIC",
    )
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1


def test_S6_ATTESTATION_无上下文_drop() -> None:
    text = "Please sign the ATTESTATION form before Friday"
    span = _span(
        "ATTESTATION", "BANK_CARD", start=text.index("ATTE"),
        source="en_regex", raw_label="SWIFT_BIC",
    )
    out = apply_precision_rules([span], text, _EMPTY)
    assert out == []


def test_S6_真实BIC_无上下文_drop() -> None:
    # 上下文 gate 是主防线：孤立出现的 8/11 位大写词一律不认
    text = "参考编号 DEUTDEFF 已登记"
    span = _span(
        "DEUTDEFF", "BANK_CARD", start=5,
        source="en_regex", raw_label="SWIFT_BIC",
    )
    out = apply_precision_rules([span], text, _EMPTY)
    assert out == []


def test_S6_beneficiary英文语境_保留() -> None:
    # good case 走查补上的缺口：英文收款语境词不在首版表内
    text = "Beneficiary bank code: CITIUS33, please double-check before transfer"
    span = _span(
        "CITIUS33", "BANK_CARD", start=text.index("CITI"),
        source="en_regex", raw_label="SWIFT_BIC",
    )
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1


def test_S6_裸bank词不足以放行() -> None:
    # 不收裸 \bbank\b：金融文本处处是 bank，否则 ATTESTATION 类误报重新放进来
    text = "The bank requires the ATTESTATION form to be notarized"
    span = _span(
        "ATTESTATION", "BANK_CARD", start=text.index("ATTE"),
        source="en_regex", raw_label="SWIFT_BIC",
    )
    out = apply_precision_rules([span], text, _EMPTY)
    assert out == []


def test_S6_BIC被NER误标ORG_救援改写() -> None:
    # e17-009 good case 实证：HanLP 把 DEUTDEFF 标 ORG 抢走 span → 救援改回 BANK_CARD
    text = "电汇路径确认：中转行 SWIFT 为 DEUTDEFF，金额按合同约定执行"
    span = _span(
        "DEUTDEFF", "ORG", start=text.index("DEUT"),
        source="hanlp", raw_label="ORG",
    )
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1
    assert out[0]["canonical"] == "BANK_CARD"
    assert out[0]["raw_label"] == "SWIFT_BIC_RESCUED"


def test_S6_普通ORG_不被BIC救援波及() -> None:
    text = "汇款给 HSBC HK 分行处理"  # 有汇款语境但形态不是 BIC
    span = _span("HSBC HK", "ORG", start=4, source="hanlp", raw_label="ORG")
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1 and out[0]["canonical"] == "ORG"


def test_S6_BIC形态ORG_无语境_不救援() -> None:
    text = "会议代号 DEUTDEFF 已分配"  # 形态命中但无汇款语境 → 保持 ORG
    span = _span("DEUTDEFF", "ORG", start=5, source="hanlp", raw_label="ORG")
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1 and out[0]["canonical"] == "ORG"


def test_S6_非BIC来源的BANK_CARD_不受gate影响() -> None:
    text = "卡号 4111111111111111"
    span = _span(
        "4111111111111111", "BANK_CARD", start=3,
        source="cn_regex", raw_label="CN_BANK_CARD",
    )
    out = apply_precision_rules([span], text, _EMPTY)
    assert len(out) == 1


# ─── 综合：非目标 kind 完全不受影响 ───


def test_其他kind_不受精度规则影响() -> None:
    text = "13800138000 和 test@example.com"
    spans = [
        _span("13800138000", "PHONE", start=0, source="cn_regex", raw_label="CN_PHONE"),
        _span("test@example.com", "EMAIL", start=14, source="cn_regex", raw_label="CN_EMAIL"),
    ]
    out = apply_precision_rules(spans, text, _EMPTY)
    assert len(out) == 2
