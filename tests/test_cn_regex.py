"""cn_regex.py 单测：4 条核心正则的命中与 validator。"""
from __future__ import annotations

import pytest

from app.detectors.cn_regex import CnRegexDetector


@pytest.fixture
def detector(canonical_map):
    return CnRegexDetector(canonical_map)


async def _detect(detector, text: str) -> list[dict]:
    return await detector.detect(text)


async def test_手机号(detector) -> None:
    spans = await _detect(detector, "联系电话 13800138000 或 13912345678。")
    nums = sorted([s["text"] for s in spans if s["canonical"] == "PHONE"])
    assert nums == ["13800138000", "13912345678"]


async def test_手机号_前后非数字(detector) -> None:
    """长数字串内的 11 位子串不应被命中"""
    spans = await _detect(detector, "订单号 138001380001234 不是手机")
    phones = [s for s in spans if s["canonical"] == "PHONE"]
    assert phones == []


async def test_邮箱(detector) -> None:
    spans = await _detect(detector, "邮箱：zhang.san+filter@example.co.uk")
    emails = [s for s in spans if s["canonical"] == "EMAIL"]
    assert len(emails) == 1
    assert emails[0]["text"] == "zhang.san+filter@example.co.uk"


async def test_邮箱_句末英文句号(detector) -> None:
    # 英文句末的 "." 不是域名的一部分（LOOK-6875 邮件正文漏识别）
    for text, want in [
        ("Please contact e-services@example.com.", "e-services@example.com"),
        ("Mail john.doe@example.co.uk. Thanks", "john.doe@example.co.uk"),
        ("见 a@example.com.cn", "a@example.com.cn"),
    ]:
        spans = await _detect(detector, text)
        assert [s["text"] for s in spans if s["canonical"] == "EMAIL"] == [want], text


async def test_身份证_18位(detector) -> None:
    spans = await _detect(detector, "身份证 11010119900101123X 已核验")
    ids = [s for s in spans if s["canonical"] == "ID_CARD"]
    assert len(ids) == 1
    assert ids[0]["text"] == "11010119900101123X"


async def test_身份证_19位不算(detector) -> None:
    spans = await _detect(detector, "数字 1101011990010112345 不是身份证")
    ids = [s for s in spans if s["canonical"] == "ID_CARD"]
    assert ids == []


async def test_dob_中文格式_保留(detector) -> None:
    spans = await _detect(detector, "出生日期 1985 年 5 月 12 日")
    dobs = [s["text"] for s in spans if s["canonical"] == "DOB"]
    assert "1985 年 5 月 12 日" in dobs


async def test_dob_ISO_出生上下文命中(detector) -> None:
    # 出生引导词后的 ISO 日期应判 DOB（只截日期，不含引导词）
    spans = await _detect(detector, "出生日期：1972-11-08")
    dobs = [s["text"] for s in spans if s["canonical"] == "DOB"]
    assert dobs == ["1972-11-08"]


async def test_dob_ISO_事件日期不误命中(detector) -> None:
    # 无出生引导词的 ISO 日期（事件/合同日期）不应判 DOB（保持原设计意图）
    spans = await _detect(detector, "2026-03-10：参与AI基金路演")
    dobs = [s["text"] for s in spans if s["canonical"] == "DOB"]
    assert dobs == []


async def test_银行卡_Luhn_通过(detector) -> None:
    # 4111111111111111 是 Visa 标准测试卡，Luhn 有效
    spans = await _detect(detector, "卡号 4111111111111111")
    bank = [s for s in spans if s["canonical"] == "BANK_CARD"]
    assert len(bank) == 1
    assert bank[0]["text"] == "4111111111111111"


async def test_银行卡_Luhn_失败被过滤(detector) -> None:
    # 4485273776936195 是 Luhn 无效
    spans = await _detect(detector, "卡号 4485273776936195")
    bank = [s for s in spans if s["canonical"] == "BANK_CARD"]
    assert bank == []


async def test_银行卡_银联19位连续_Luhn有效(detector) -> None:
    # 6222021234567890128：银联 62 开头 19 位借记卡，Luhn 有效（合成测试号，非真人卡）
    # 国内最常见卡号长度；cn_regex 正则 \d{13,19} 覆盖，补 19 位连续场景
    spans = await _detect(detector, "王建国的银行卡号是6222021234567890128")
    texts = [s["text"] for s in spans if s["canonical"] == "BANK_CARD"]
    assert texts == ["6222021234567890128"]


async def test_银行卡_银联19位_Luhn无效拒绝(detector) -> None:
    # 6222021234567890123：末位非合法校验位，Luhn 无效（此前 e2e 误用、被正确拒绝的卡号）
    spans = await _detect(detector, "卡号 6222021234567890123")
    bank = [s for s in spans if s["canonical"] == "BANK_CARD"]
    assert bank == []


async def test_canonical_与_raw_label(detector) -> None:
    spans = await _detect(detector, "电话 13800138000")
    assert spans[0]["canonical"] == "PHONE"
    assert spans[0]["raw_label"] == "CN_PHONE"
    assert spans[0]["source"] == "cn_regex"


async def test_text_切片与_start_end_一致(detector) -> None:
    text = "手机 13800138000，邮箱 a@b.com"
    spans = await _detect(detector, text)
    for s in spans:
        assert text[s["start"]:s["end"]] == s["text"]


async def test_F2_business_id_客户号(detector) -> None:
    spans = await _detect(detector, "客户王志远（客户号 CST-00012345）已确认")
    bids = [s for s in spans if s["canonical"] == "BUSINESS_ID"]
    assert len(bids) == 1
    assert bids[0]["text"] == "CST-00012345"


async def test_F2_business_id_合同号_多段(detector) -> None:
    spans = await _detect(detector, "合同号 HT-2026-0512-001 项下双方签字")
    bids = [s for s in spans if s["canonical"] == "BUSINESS_ID"]
    assert len(bids) == 1
    assert bids[0]["text"] == "HT-2026-0512-001"


async def test_F2_business_id_LP_协议(detector) -> None:
    spans = await _detect(detector, "LP 协议 LP-SCC-2023-A07 已签字")
    bids = [s for s in spans if s["canonical"] == "BUSINESS_ID"]
    assert bids[0]["text"] == "LP-SCC-2023-A07"


async def test_F2_business_id_多个混合(detector) -> None:
    text = "客户号 CST-00045678 关联订单 ORD-CRYPTO-20260520-009 与交易 TRD-CN-FX-20260520-0001"
    spans = await _detect(detector, text)
    texts = sorted(s["text"] for s in spans if s["canonical"] == "BUSINESS_ID")
    assert texts == ["CST-00045678", "ORD-CRYPTO-20260520-009", "TRD-CN-FX-20260520-0001"]


async def test_F2_business_id_不命中无前缀的纯数字(detector) -> None:
    """BVI 公司号 2099876 这种纯数字无前缀 → 不命中（应由 NVIDIA 兜底，超出 F2 范围）"""
    spans = await _detect(detector, "BVI Co. No. 2099876")
    bids = [s for s in spans if s["canonical"] == "BUSINESS_ID"]
    assert bids == []


async def test_F2_business_id_不误识普通中划线词(detector) -> None:
    """COVID-19、PE-30x、Q1-Q4 等不应被命中"""
    spans = await _detect(detector, "PE-30x 偏高，COVID-19 影响 Q1-Q4 数据")
    bids = [s for s in spans if s["canonical"] == "BUSINESS_ID"]
    assert bids == []


async def test_F2_business_id_港股公司号(detector) -> None:
    spans = await _detect(detector, "公司编号 CR-1234567 已登记")
    bids = [s for s in spans if s["canonical"] == "BUSINESS_ID"]
    assert bids[0]["text"] == "CR-1234567"


async def test_F2_business_id_jersey_trust(detector) -> None:
    spans = await _detect(detector, "Trust code TR-JE-2026-018")
    bids = [s for s in spans if s["canonical"] == "BUSINESS_ID"]
    assert bids[0]["text"] == "TR-JE-2026-018"


async def test_F6_HK_dash_account(detector) -> None:
    spans = await _detect(detector, "汇丰账号 808-123456-001 @HSBC HK")
    accs = [s for s in spans if s["canonical"] == "ACCOUNT"]
    assert accs[0]["text"] == "808-123456-001"


async def test_F6_SG_dash_account(detector) -> None:
    spans = await _detect(detector, "DBS 账号 003-987654-321")
    accs = [s for s in spans if s["canonical"] == "ACCOUNT"]
    assert accs[0]["text"] == "003-987654-321"


async def test_F6_long_contiguous_account(detector) -> None:
    spans = await _detect(detector, "对公账号 622188000000012345678 已开户")
    accs = [s for s in spans if s["canonical"] == "ACCOUNT"]
    assert accs[0]["text"] == "622188000000012345678"


async def test_F6_investor_code(detector) -> None:
    spans = await _detect(detector, "投资人代码 A0987654321 已激活")
    accs = [s for s in spans if s["canonical"] == "ACCOUNT"]
    assert accs[0]["text"] == "A0987654321"


async def test_F6_account_HK_not_collide_with_business_id(detector) -> None:
    """CST-00012345 是 BUSINESS_ID（字母开头），与 HK 808-123456-001（数字开头）不冲突"""
    spans = await _detect(detector, "客户号 CST-00012345 关联账户 808-234567-002")
    bids = [s["text"] for s in spans if s["canonical"] == "BUSINESS_ID"]
    accs = [s["text"] for s in spans if s["canonical"] == "ACCOUNT"]
    assert "CST-00012345" in bids
    assert "808-234567-002" in accs


async def test_F8_EIN_detected_as_business_id(detector) -> None:
    """98-7654321 不含 TIN 上下文 → BUSINESS_ID（service 端到端再由 context-rule 决定是否提升）"""
    spans = await _detect(detector, "Delaware EIN 98-7654321 已注册")
    bids = [s for s in spans if s["canonical"] == "BUSINESS_ID" and s["text"] == "98-7654321"]
    assert len(bids) == 1


# ─── F2b USCC 主检测（补字母型 USCC 漏检）───
async def test_USCC_字母型主检测命中(detector) -> None:
    spans = await _detect(
        detector, "统一社会信用代码 91310115MA1K3Y2P5R，法人代表周建华"
    )
    uscc = [
        s for s in spans
        if s["canonical"] == "BUSINESS_ID" and s["text"] == "91310115MA1K3Y2P5R"
    ]
    assert len(uscc) == 1


async def test_USCC_不误吃身份证(detector) -> None:
    """18 位纯数字 / 末位 X 身份证不应被 CN_USCC 命中（validator 要求前17位含字母）。"""
    spans = await _detect(
        detector, "身份证 110101199001011234 和 11010119900101123X"
    )
    assert [s for s in spans if s["raw_label"] == "CN_USCC"] == []


# ─── F10 中文地址结构化检测（整段命中，治碎片化）───
async def test_地址_结构化整段命中(detector) -> None:
    spans = await _detect(detector, "工作地址在上海市浦东新区世纪大道100号，附近")
    addr = [s for s in spans if s["canonical"] == "ADDRESS"]
    assert len(addr) == 1
    assert "上海市浦东新区世纪大道100号" in addr[0]["text"]


async def test_地址_带空格门牌(detector) -> None:
    spans = await _detect(detector, "名下房产：上海市徐汇区漕溪北路 38 号，市值约 1500 万")
    addr = [s for s in spans if s["canonical"] == "ADDRESS"]
    assert len(addr) == 1
    assert "漕溪北路" in addr[0]["text"] and "38" in addr[0]["text"]


async def test_地址_不误命中非地址(detector) -> None:
    for neg in ["可投 800 万，期限 5 年", "丝绸之路 2000 年开通", "上海市场监管局发布"]:
        spans = await _detect(detector, neg)
        assert [s for s in spans if s["canonical"] == "ADDRESS"] == [], neg


async def test_地址_尾部号后非地址名词不吸(detector) -> None:
    # 门牌「N 号」后紧跟非地址名词（线/文件/公告/台风…）属歧义，不得吸进地址。
    for text, must_end in [
        ("公司在浦东新区张江路 99 号 8 号线地铁口附近", "99 号"),
        ("注册地南京路 100 号 3 号文件已存档", "100 号"),
        ("办公地中山路 50 号 5 号公告栏旁", "50 号"),
    ]:
        spans = await _detect(detector, text)
        addr = [s for s in spans if s["canonical"] == "ADDRESS"]
        assert len(addr) == 1, text
        assert addr[0]["text"].endswith(must_end), addr[0]["text"]


async def test_地址_合法号楼号室仍吸(detector) -> None:
    # 「N 号楼」「N 号」(房号，后接句末/标点) 是合法地址尾段，应保留。
    spans = await _detect(detector, "住址中山路 12 号 5 号楼 302 室。")
    addr = [s for s in spans if s["canonical"] == "ADDRESS"]
    assert len(addr) == 1
    assert "5 号楼" in addr[0]["text"] and "302 室" in addr[0]["text"]


# ── LOOK-6827：身份证改为「首位 1–6 + 出生日期合法」，不强制校验位 ──

async def test_身份证_首位非法不算(detector) -> None:
    spans = await _detect(detector, "单号 012345678901234567 已处理")
    assert not [s for s in spans if s["canonical"] == "ID_CARD"]


async def test_身份证_出生日期非法不算(detector) -> None:
    spans = await _detect(detector, "编号 110101199013401234 和 110101199002301234")
    assert not [s for s in spans if s["canonical"] == "ID_CARD"]


async def test_身份证_校验位错误仍识别(detector) -> None:
    spans = await _detect(detector, "身份证 110101199001011235 已核验")
    ids = [s for s in spans if s["canonical"] == "ID_CARD"]
    assert [s["text"] for s in ids] == ["110101199001011235"]



async def test_身份证_港澳台居住证与外国人永居证识别(detector) -> None:
    # 评审 P1：首位只放 1–6 会漏掉 81/82/83 开头的港澳台居民居住证与 9 开头的外国人永居证
    spans = await _detect(
        detector, "居住证 810000199001011234、820000199001011234、830000199001011234，永居证 911156199001011234"
    )
    ids = {s["text"] for s in spans if s["canonical"] == "ID_CARD"}
    assert ids == {
        "810000199001011234",
        "820000199001011234",
        "830000199001011234",
        "911156199001011234",
    }


async def test_手机号_3_4_4分组(detector) -> None:
    # 邮件里常见的空格/短横线分组写法
    text = "手机：138 0176 5924    备用：139-1728-4410"
    phones = [s["text"] for s in await _detect(detector, text) if s["canonical"] == "PHONE"]
    assert phones == ["138 0176 5924", "139-1728-4410"]


async def test_手机号_分组_长数字串内不命中(detector) -> None:
    for text in ["卡号 6225 138 0176 5924", "编号 138 0176 59241"]:
        phones = [s for s in await _detect(detector, text) if s["canonical"] == "PHONE"]
        assert phones == [], text


async def test_座机(detector) -> None:
    for text, want in [
        ("直拨 021-6888 7102", "021-6888 7102"),
        ("电话 021-68887102", "021-68887102"),
        ("Tel: 010 8888 6666", "010 8888 6666"),
        ("总机 (0755)2688-1234", "(0755)2688-1234"),
        ("总机（0571）8765432", "（0571）8765432"),
        ("电话 021-68887102-801", "021-68887102-801"),
        ("电话 021-6888 7102 转 801", "021-6888 7102 转 801"),
        # 英文签名：标签后直接跟空格
        ("Tel 021-6888 7102", "021-6888 7102"),
        ("Fax 0755 2688 1234", "0755 2688 1234"),
        ("Phone (021) 6888 7102", "(021) 6888 7102"),
    ]:
        phones = [s["text"] for s in await _detect(detector, text) if s["canonical"] == "PHONE"]
        assert phones == [want], text


async def test_座机_不误命中(detector) -> None:
    for text in [
        "日期 2026-11-05",
        "编号 02168887102",
        "金额 012-3456-7890",
        "账号 028-123456-001",
        "账号 0-123-456789-0",
        "单号 021-1234 5678",
        "收款账户 DE44 5001 0517 5407 3249 31",
    ]:
        phones = [s for s in await _detect(detector, text) if s["canonical"] == "PHONE"]
        assert phones == [], text


async def test_号码后紧跟另一串数字仍识别(detector) -> None:
    # 表格/签名里号码后面常跟另一个号码或日期
    for text, want in [
        ("联系 021-6888 7102 13801765924", ["021-6888 7102", "13801765924"]),
        ("021-6888 7102 2026-11-05 到期", ["021-6888 7102"]),
        ("手机 138 0176 5924 2026-11-05 到期", ["138 0176 5924"]),
    ]:
        phones = sorted(s["text"] for s in await _detect(detector, text) if s["canonical"] == "PHONE")
        assert phones == sorted(want), text
